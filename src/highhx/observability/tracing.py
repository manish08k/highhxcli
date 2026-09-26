"""Lightweight tracing: nested timed spans stored locally."""

from __future__ import annotations

import contextlib
import contextvars
import json
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from highhx.storage.database import Database
from highhx.utils.time import iso_now

_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar("highhx_span", default=None)


@dataclass
class Span:
    """A timed unit of work."""

    trace_id: str
    span_id: str
    name: str
    parent_id: str | None = None
    started_at: str = field(default_factory=iso_now)
    duration: float | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "started_at": self.started_at,
            "duration": self.duration,
            "status": self.status,
            "attributes": self.attributes,
            "children": [child.to_dict() for child in self.children],
        }


class Tracer:
    """Creates spans for one trace and persists them when a database is available."""

    def __init__(self, db: Database | None = None, trace_id: str | None = None) -> None:
        self.db = db
        self.trace_id = trace_id or secrets.token_hex(8)

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = _current_span.get()
        span = Span(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(4),
            name=name,
            parent_id=parent.span_id if parent and parent.trace_id == self.trace_id else None,
            attributes=dict(attributes),
        )
        token = _current_span.set(span)
        began = time.monotonic()
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.attributes.setdefault("error", type(exc).__name__)
            raise
        finally:
            span.duration = time.monotonic() - began
            _current_span.reset(token)
            self._persist(span)

    def record(
        self, name: str, duration: float, *, status: str = "ok", parent_id: str | None = None, **attributes: Any
    ) -> Span:
        """Record an already-finished span (e.g. a step timed by another thread)."""
        span = Span(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(4),
            name=name,
            parent_id=parent_id,
            duration=duration,
            status=status,
            attributes=dict(attributes),
        )
        self._persist(span)
        return span

    def _persist(self, span: Span) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO spans (trace_id, span_id, parent_id, name, started_at, duration, status, attributes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                span.trace_id,
                span.span_id,
                span.parent_id,
                span.name,
                span.started_at,
                span.duration,
                span.status,
                json.dumps(span.attributes, default=str),
            ),
        )


def load_trace(db: Database, trace_id: str) -> list[Span]:
    """Load a trace as a forest of root spans (children nested)."""
    rows = db.query("SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, rowid", (trace_id,))
    spans = {
        row["span_id"]: Span(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            name=row["name"],
            parent_id=row["parent_id"],
            started_at=row["started_at"],
            duration=row["duration"],
            status=row["status"] or "ok",
            attributes=json.loads(row["attributes"] or "{}"),
        )
        for row in rows
    }
    roots: list[Span] = []
    for span in spans.values():
        parent = spans.get(span.parent_id) if span.parent_id else None
        if parent is not None:
            parent.children.append(span)
        else:
            roots.append(span)
    return roots
