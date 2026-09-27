"""Resumable AI streams, shared by every platform instance through the database.

One :class:`StreamRun` per (account, idempotency key) runs the upstream model call exactly
once, on the instance that claimed the key, and appends its events (with sequential ids)
to ``ai_stream_events``. HTTP responses are *readers* that replay those events from the
database, so a client that loses its connection can reconnect to **any** instance with
the same key and ``Last-Event-ID`` and receive only the events it missed — the model is
not called again and usage is metered once.

Coordination is through the ``ai_streams`` row:

- the owner heartbeats it; readers fail a stream whose owner stopped heartbeating
  (the instance died), so a client never waits forever;
- readers record that a client is connected; the owner cancels the upstream call when no
  client has been connected for ``grace`` seconds (no zombie upstream calls);
- the cancel endpoint (on any instance) records a cancellation request the owner acts on.

Finished streams stay replayable for ``retention`` seconds. Instances need reasonably
synchronised clocks (NTP); the timeouts are seconds, not milliseconds.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from highhx.cloud import sse
from highhx.execution.cancellation import CancellationToken
from highhx_platform.accounts import _aware
from highhx_platform.db import Database
from highhx_platform.models import AiStream, AiStreamEvent, UsageRecord, utcnow

log = logging.getLogger("highhx_platform.streams")


def instance_id() -> str:
    return f"{socket.gethostname()[:60]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Snapshot:
    frames: list[bytes]
    done: bool
    owner_stale: bool


class StreamRun:
    """The owner's side of one stream: the upstream worker appends events here."""

    def __init__(self, store: StreamStore, request_id: str) -> None:
        self.store = store
        self.request_id = request_id
        self.cancel = CancellationToken()
        self.seq = 0
        self.status = "running"
        self.done = False
        self._stop = threading.Event()

    def append(self, event: str, data: dict[str, Any]) -> int:
        self.seq += 1
        frame = sse.encode(event, data, event_id=self.seq).decode("utf-8")
        with self.store.db.sessions() as session:
            session.add(AiStreamEvent(request_id=self.request_id, seq=self.seq, frame=frame))
            session.commit()
        return self.seq

    def finish(self, status: str) -> None:
        self.status, self.done = status, True
        self._stop.set()
        with self.store.db.sessions() as session:
            session.execute(
                update(AiStream)
                .where(AiStream.request_id == self.request_id)
                .values(status=status, done=True, finished_at=utcnow(), heartbeat_at=utcnow())
            )
            session.commit()

    def watch(self) -> None:
        """Heartbeat; act on cancellation requests and on clients that went away."""
        interval = self.store.interval
        while not self._stop.wait(interval):
            try:
                now = utcnow()
                with self.store.db.sessions() as session:
                    session.execute(
                        update(AiStream).where(AiStream.request_id == self.request_id).values(heartbeat_at=now)
                    )
                    row = session.execute(
                        select(AiStream.cancel_requested_at, AiStream.reader_seen_at).where(
                            AiStream.request_id == self.request_id
                        )
                    ).one_or_none()
                    session.commit()
            except Exception:  # a database hiccup must not kill the heartbeat
                log.exception("stream heartbeat failed")
                continue
            if row is None:
                continue
            cancel_requested_at, reader_seen_at = row
            if cancel_requested_at is not None:
                self.cancel.cancel("cancelled by client")
            elif _aware(reader_seen_at) < now - timedelta(seconds=self.store.grace):
                self.cancel.cancel("abandoned: no client connected")


class StreamStore:
    def __init__(
        self,
        db: Database,
        *,
        grace: float = 30.0,
        retention: float = 600.0,
        owner_timeout: float = 15.0,
        poll: float = 0.1,
        owner: str | None = None,
    ) -> None:
        self.db = db
        self.grace = grace
        self.retention = retention
        self.owner_timeout = owner_timeout
        self.poll = poll
        self.owner = owner or instance_id()
        self._last_prune = 0.0

    @property
    def interval(self) -> float:
        """Heartbeat / reader-presence interval: well inside the grace period."""
        return max(0.02, min(1.0, self.grace / 4))

    # ----------------------------------------------------------------- lookup
    def get(self, request_id: str) -> AiStream | None:
        with self.db.sessions() as session:
            return session.get(AiStream, request_id)

    def new_row(self, request_id: str, user_id: str, provider: str) -> AiStream:
        """The stream row, added to the caller's transaction together with the usage claim."""
        return AiStream(request_id=request_id, user_id=user_id, owner=self.owner, provider=provider)

    # ----------------------------------------------------------------- owner
    def start(self, request_id: str, worker: Callable[[StreamRun], None]) -> StreamRun:
        run = StreamRun(self, request_id)
        name = request_id[-12:]
        threading.Thread(target=run.watch, name=f"stream-watch-{name}", daemon=True).start()
        threading.Thread(target=worker, args=(run,), name=f"stream-{name}", daemon=True).start()
        return run

    # ----------------------------------------------------------------- readers
    def read(self, request_id: str, after: int, *, touch: bool) -> Snapshot:
        """Events after ``after`` (the last id the client saw). ``done`` is read *before* the
        events, so a finished stream's snapshot always contains all of its events."""
        with self.db.sessions() as session:
            if touch:
                session.execute(
                    update(AiStream).where(AiStream.request_id == request_id).values(reader_seen_at=utcnow())
                )
                session.commit()
            row = session.execute(
                select(AiStream.done, AiStream.heartbeat_at).where(AiStream.request_id == request_id)
            ).one_or_none()
            if row is None:
                return Snapshot([], True, False)
            done, heartbeat_at = row
            frames = session.scalars(
                select(AiStreamEvent.frame)
                .where(AiStreamEvent.request_id == request_id, AiStreamEvent.seq > after)
                .order_by(AiStreamEvent.seq)
                .limit(1000)
            ).all()
        stale = not done and _aware(heartbeat_at) < utcnow() - timedelta(seconds=self.owner_timeout)
        return Snapshot([f.encode("utf-8") for f in frames], bool(done), stale)

    def fail_orphan(self, request_id: str) -> bool:
        """The owning instance stopped heartbeating: end the stream with an error, exactly once."""
        cutoff = utcnow() - timedelta(seconds=self.owner_timeout)
        with self.db.sessions() as session:
            claimed = session.execute(
                update(AiStream)
                .where(AiStream.request_id == request_id, AiStream.done.is_(False), AiStream.heartbeat_at < cutoff)
                .values(done=True, status="error", finished_at=utcnow())
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                session.rollback()
                return False
            last = session.scalar(select(func.max(AiStreamEvent.seq)).where(AiStreamEvent.request_id == request_id))
            seq = int(last or 0) + 1
            data = {
                "code": "upstream_error",
                "message": "The platform instance handling this request stopped.",
                "retryable": True,
            }
            frame = sse.encode("error", data, event_id=seq).decode("utf-8")
            session.add(AiStreamEvent(request_id=request_id, seq=seq, frame=frame))
            session.execute(
                update(UsageRecord)
                .where(UsageRecord.request_id == request_id, UsageRecord.status == "pending")
                .values(status="error", finished_at=utcnow())
            )
            session.commit()
        log.warning("stream %s: owner stopped heartbeating; failed it", request_id)
        return True

    # ----------------------------------------------------------------- control
    def request_cancel(self, request_id: str, user_id: str) -> str | None:
        """Record a cancellation request for one of ``user_id``'s streams. Returns its status."""
        with self.db.sessions() as session:
            row = session.get(AiStream, request_id)
            if row is None or row.user_id != user_id:
                return None
            if not row.done and row.cancel_requested_at is None:
                row.cancel_requested_at = utcnow()
            session.commit()
            return "cancelling" if not row.done else row.status

    def prune(self, *, force: bool = False) -> int:
        """Drop the events of streams finished more than ``retention`` seconds ago."""
        now = time.monotonic()
        if not force and now - self._last_prune < 30:
            return 0
        self._last_prune = now
        cutoff = utcnow() - timedelta(seconds=self.retention)
        with self.db.sessions() as session:
            expired = select(AiStream.request_id).where(
                AiStream.done.is_(True), AiStream.events_pruned.is_(False), AiStream.finished_at <= cutoff
            )
            ids = list(session.scalars(expired).all())
            if ids:
                session.execute(delete(AiStreamEvent).where(AiStreamEvent.request_id.in_(ids)))
                session.execute(update(AiStream).where(AiStream.request_id.in_(ids)).values(events_pruned=True))
            session.commit()
        return len(ids)
