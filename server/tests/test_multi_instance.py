"""Two platform instances that share only the database (as replicas behind a load balancer).

Each instance is a separate application with its own engine, stream store, rate limiter
and uvicorn server on its own port; nothing is shared in memory. Runs on SQLite and PostgreSQL.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from helpers import GatedUpstream, LiveServer, auth, text_reply
from highhx.agent.streaming import TextDelta
from highhx_platform.app import create_app
from highhx_platform.config import Settings
from highhx_platform.models import AiStream, UsageRecord, User, utcnow
from test_hardening import DONE, body

PASSWORD = "correct horse battery"


@contextmanager
def two_instances(settings: Settings, gated: GatedUpstream) -> Iterator[tuple[Any, LiveServer, LiveServer]]:
    apps = [create_app(settings, provider_factory=gated.factory) for _ in range(2)]
    assert apps[0].state.streams.owner != apps[1].state.streams.owner
    with ExitStack() as stack:
        a, b = (stack.enter_context(LiveServer(app)) for app in apps)
        yield apps, a, b
    for app in apps:
        app.state.db.engine.dispose()


def pro_token(server: LiveServer) -> str:
    email = f"mi{time.monotonic_ns()}@example.com"
    status, data = server.json("POST", "/v1/auth/signup", {"email": email, "password": PASSWORD})
    assert status == 201, data
    server.json("POST", "/v1/admin/plan", {"email": email, "plan": "pro"}, {"x-admin-token": "admin-secret"})
    return str(data["access_token"])


def usage(app: Any) -> list[UsageRecord]:
    with app.state.db.sessions() as s:
        return list(s.scalars(select(UsageRecord)).all())


def wait_for(predicate: Any, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_disconnect_from_a_and_resume_on_b(settings: Settings) -> None:
    """instance A streams → client drops → client reconnects to instance B → only the missed
    events; the model is called once and metered once; no event is lost or repeated."""
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), TextDelta("c"), DONE])
    with two_instances(settings, gated) as (apps, a, b):
        token = pro_token(a)
        headers = {**auth(token), "Idempotency-Key": "multi-0001"}
        gated.release(2)
        first = a.stream(body(), headers, limit=2)
        assert [e.id for e in first] == [1, 2]
        gated.release()
        rest = b.stream(body(), {**headers, "Last-Event-ID": "2"})
        assert [e.id for e in rest] == [3, 4] and rest[-1].event == "completed"
        assert [e.data.get("text") for e in first + rest[:-1]] == ["a", "b", "c"]
        assert gated.calls == 1
        rows = usage(apps[1])
        assert len(rows) == 1
        assert wait_for(lambda: usage(apps[0])[0].status == "ok")
        # A full replay from B after completion also works and still does not call the model.
        replay = b.stream(body(), headers)
        assert [e.id for e in replay] == [1, 2, 3, 4] and gated.calls == 1


def test_concurrent_duplicates_on_both_instances_call_the_model_once(settings: Settings) -> None:
    gated = GatedUpstream([TextDelta("a"), DONE])
    with two_instances(settings, gated) as (apps, a, b):
        token = pro_token(a)
        headers = {**auth(token), "Idempotency-Key": "multi-dup-01"}
        results: list[list[Any]] = []
        lock = threading.Lock()

        def call(server: LiveServer) -> None:
            try:
                events: Any = server.stream(body(), headers)
            except AssertionError as exc:
                events = str(exc)
            with lock:
                results.append(events)

        threads = [threading.Thread(target=call, args=(s,)) for s in (a, b, a, b)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        gated.release()
        for t in threads:
            t.join(15)
        assert gated.calls == 1
        assert len(results) == 4 and all(r and r[-1].event == "completed" for r in results), results
        assert len(usage(apps[0])) == 1


def test_cancel_on_b_stops_the_upstream_running_on_a(settings: Settings) -> None:
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), DONE])
    with two_instances(settings, gated) as (apps, a, b):
        token = pro_token(a)
        headers = {**auth(token), "Idempotency-Key": "multi-cancel"}
        gated.release(1)
        collected: list[Any] = []
        reader = threading.Thread(target=lambda: collected.extend(a.stream(body(), headers)))
        reader.start()
        assert wait_for(lambda: len(usage(apps[0])) == 1)
        status, result = b.json("POST", "/v1/ai/messages/multi-cancel/cancel", None, auth(token))
        assert status == 200 and result["cancelled"]
        assert gated.cancelled.wait(10), "the upstream on instance A kept running"
        reader.join(10)
        assert collected[-1].event == "error" and collected[-1].data["code"] == "cancelled"
        assert wait_for(lambda: usage(apps[0])[0].status == "cancelled")
        # Another account cannot cancel it (or learn it exists).
        other = pro_token(b)
        status, result = b.json("POST", "/v1/ai/messages/multi-cancel/cancel", None, auth(other))
        assert result == {"cancelled": False, "status": "unknown"}


def test_abandoned_stream_is_cancelled_even_when_nobody_reconnects(settings: Settings) -> None:
    settings.stream_resume_grace = 0.4
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), DONE])
    with two_instances(settings, gated) as (apps, a, _b):
        token = pro_token(a)
        gated.release(1)
        a.stream(body(), {**auth(token), "Idempotency-Key": "multi-aband"}, limit=1)
        assert gated.cancelled.wait(10)
        assert wait_for(lambda: usage(apps[0])[0].status == "cancelled")


def test_reader_on_b_keeps_a_stream_alive_that_a_started(settings: Settings) -> None:
    """A client connected to B counts as present for the owner on A (no false abandonment)."""
    settings.stream_resume_grace = 0.4
    gated = GatedUpstream([TextDelta("a"), TextDelta("b"), DONE])
    with two_instances(settings, gated) as (_apps, a, b):
        token = pro_token(a)
        headers = {**auth(token), "Idempotency-Key": "multi-alive"}
        gated.release(1)
        a.stream(body(), headers, limit=1)
        collected: list[Any] = []
        reader = threading.Thread(target=lambda: collected.extend(b.stream(body(), {**headers, "Last-Event-ID": "1"})))
        reader.start()
        time.sleep(1.2)  # three grace periods with the client on B only
        assert not gated.cancelled.is_set()
        gated.release()
        reader.join(10)
        assert [e.id for e in collected] == [2, 3] and collected[-1].event == "completed"


def test_stream_of_a_dead_instance_fails_instead_of_hanging(settings: Settings) -> None:
    """The owner stopped heartbeating (instance crashed): a reader on another instance ends the
    stream with one retryable error and the pending usage record is closed — exactly once."""
    settings.stream_resume_grace = 60
    gated = GatedUpstream([TextDelta("a"), DONE])
    with two_instances(settings, gated) as (apps, a, b):
        token = pro_token(a)
        with apps[0].state.db.sessions() as s:
            user = s.scalars(select(User)).one()
            request_id = f"{user.id}:dead-owner1"
            s.add(AiStream(request_id=request_id, user_id=user.id, owner="gone:1:x", provider="anthropic"))
            s.add(
                UsageRecord(user_id=user.id, request_id=request_id, provider="anthropic", model="m", status="pending")
            )
            s.commit()
            s.execute(
                update(AiStream)
                .where(AiStream.request_id == request_id)
                .values(heartbeat_at=utcnow() - timedelta(minutes=5))
            )
            s.commit()
        results: list[list[Any]] = []
        threads = [
            threading.Thread(
                target=lambda s=s: results.append(s.stream(body(), {**auth(token), "Idempotency-Key": "dead-owner1"}))
            )
            for s in (a, b)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert all(r and r[-1].event == "error" and r[-1].data["retryable"] for r in results)
        assert results[0][-1].id == results[1][-1].id == 1  # one error event, not two
        assert usage(apps[0])[0].status == "error" and gated.calls == 0


def test_rate_limits_are_shared_between_instances(settings: Settings) -> None:
    settings.login_attempts_per_15min = 4
    gated = GatedUpstream([DONE])
    with two_instances(settings, gated) as (_apps, a, b):
        pro_token(a)
        codes = [
            server.json("POST", "/v1/auth/login", {"email": "nobody@example.com", "password": "wrong password!"})[0]
            for server in (a, b, a, b, a, b)
        ]
        assert codes[:4] == [401, 401, 401, 401]
        assert codes[4:] == [429, 429], codes


def test_normal_reply_through_either_instance(settings: Settings) -> None:
    gated = GatedUpstream(text_reply("hello"))
    gated.release()
    with two_instances(settings, gated) as (_apps, a, b):
        token = pro_token(b)
        events = b.stream(body(), {**auth(token), "Idempotency-Key": "multi-plain"})
        assert [e.event for e in events] == ["text", "completed"]
        assert a.json("GET", "/v1/usage", None, auth(token))[0] == 200
