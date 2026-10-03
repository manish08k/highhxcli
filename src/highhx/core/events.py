"""A small, thread-safe in-process event bus."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import secrets
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from highhx.utils.time import iso_now

log = logging.getLogger(__name__)

WILDCARD = "*"


CONTEXT_KEYS = ("trace_id", "session_id", "task_id", "action_id", "source")
"""The envelope every event carries when it is emitted inside :func:`trace_context`."""

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("highhx_event_context", default=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def current_context() -> dict[str, str]:
    """The trace envelope of the code running now (empty outside any :func:`trace_context`)."""
    return dict(_context.get() or {})


@contextlib.contextmanager
def trace_context(**values: str | None) -> Iterator[dict[str, str]]:
    """Stamp every event emitted inside the block with these ids (nested blocks add to and
    override the outer ones). Unknown keys are refused so the envelope stays a fixed schema."""
    unknown = set(values) - set(CONTEXT_KEYS)
    if unknown:
        raise ValueError(f"unknown event context keys: {', '.join(sorted(unknown))}")
    merged = {**(_context.get() or {}), **{k: str(v) for k, v in values.items() if v}}
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


@dataclass(frozen=True)
class Event:
    """An event emitted by the runtime."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=iso_now)
    context: dict[str, str] = field(default_factory=dict)
    """trace_id, session_id, task_id, action_id, source — whichever were set when it was emitted."""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "timestamp": self.timestamp, **self.context, "payload": dict(self.data)}


Handler = Callable[[Event], None]


class EventBus:
    """Publish/subscribe dispatcher. Handler exceptions are logged, never propagated."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, name: str, handler: Handler) -> Callable[[], None]:
        """Register ``handler`` for ``name`` (or ``*`` for all). Returns an unsubscribe function."""
        with self._lock:
            self._handlers[name].append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers[name]:
                    self._handlers[name].remove(handler)

        return _unsubscribe

    def emit(self, name: str, /, **data: Any) -> Event:
        """Emit an event synchronously to all matching handlers."""
        event = Event(name=name, data=data, context=current_context())
        with self._lock:
            handlers = [*self._handlers.get(name, ()), *self._handlers.get(WILDCARD, ())]
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # pragma: no cover - defensive
                log.exception("event handler failed for %s", name)
        return event


# Well-known event names.
COMMAND_STARTED = "command.started"
COMMAND_OUTPUT = "command.output"
COMMAND_FINISHED = "command.finished"
COMMAND_RETRY = "command.retry"
STEP_STARTED = "step.started"
STEP_FINISHED = "step.finished"
WORKFLOW_STARTED = "workflow.started"
WORKFLOW_FINISHED = "workflow.finished"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
