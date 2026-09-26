"""A small, thread-safe in-process event bus."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from highhx.utils.time import iso_now

log = logging.getLogger(__name__)

WILDCARD = "*"


@dataclass(frozen=True)
class Event:
    """An event emitted by the runtime."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=iso_now)


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
        event = Event(name=name, data=data)
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
