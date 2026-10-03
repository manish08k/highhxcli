"""The event stream: every runtime event with its trace envelope, for the TUI, traces and plugins.

    with trace_context(trace_id=…, task_id=…, source="agent"):   # highhx.core.events
        executor.run(…)          # action.planned / approval.* / action.* carry the ids
        bus.emit("grounding.completed", strategy="dom", …)

    recorder = EventRecorder.attach(app.ctx.events)          # a bounded, thread-safe buffer
    recorder.since(cursor)  ·  recorder.for_trace(trace_id)  ·  recorder.snapshot()

Consumers only read events. The live dashboard (:mod:`highhx.ui.live`), task traces
(:mod:`highhx.observability.tasktrace`), benchmark metrics and plugins all get the same records,
so none of them needs an execution hook of its own. Payloads are redacted before they are kept
when a redactor is given, which is the case for everything that is written to disk.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.core.events import CONTEXT_KEYS, WILDCARD, Event, EventBus

if TYPE_CHECKING:
    from highhx.security.secrets import Redactor

DEFAULT_CAPACITY = 5000


@dataclass(frozen=True)
class EventRecord:
    """One event as consumers see it: name, time, envelope and payload (plain JSON values)."""

    seq: int
    name: str
    timestamp: str
    payload: dict[str, Any]
    trace_id: str = ""
    session_id: str = ""
    task_id: str = ""
    action_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"seq": self.seq, "event": self.name, "ts": self.timestamp}
        for key in CONTEXT_KEYS:
            value = getattr(self, key)
            if value:
                out[key] = value
        out["payload"] = self.payload
        return out


def plain(value: Any, depth: int = 0) -> Any:
    """``value`` as JSON-safe data (objects with ``to_dict`` are expanded, depth and size bounded)."""
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if hasattr(value, "to_dict"):
        try:
            return plain(value.to_dict(), depth + 1)
        except Exception:  # a broken to_dict must not break the stream
            return str(value)
    if isinstance(value, dict):
        return {str(k): plain(v, depth + 1) for k, v in list(value.items())[:200]}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v, depth + 1) for v in list(value)[:200]]
    return str(value)


@dataclass
class EventRecorder:
    """Keeps the most recent ``capacity`` events. Subscribers are called after each one is kept."""

    capacity: int = DEFAULT_CAPACITY
    redactor: Redactor | None = None
    _records: deque[EventRecord] = field(init=False, repr=False)
    _seq: int = field(init=False, default=0, repr=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    _listeners: list[Callable[[EventRecord], None]] = field(init=False, default_factory=list, repr=False)
    _detach: Callable[[], None] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._records = deque(maxlen=self.capacity)

    @classmethod
    def attach(cls, bus: EventBus, *, capacity: int = DEFAULT_CAPACITY, redactor: Redactor | None = None) -> EventRecorder:
        recorder = cls(capacity, redactor)
        recorder._detach = bus.subscribe(WILDCARD, recorder.on_event)
        return recorder

    def close(self) -> None:
        if self._detach is not None:
            self._detach()
            self._detach = None

    def listen(self, callback: Callable[[EventRecord], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return remove

    def on_event(self, event: Event) -> None:
        self.record(event)

    def record(self, event: Event) -> EventRecord:
        payload = plain(event.data)
        if self.redactor is not None:
            payload = json.loads(self.redactor.redact(json.dumps(payload, default=str)))
        with self._lock:
            self._seq += 1
            item = EventRecord(
                self._seq,
                event.name,
                event.timestamp,
                payload,
                **{k: str(event.context.get(k, "")) for k in CONTEXT_KEYS},
            )
            self._records.append(item)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(item)
            except Exception:  # a broken consumer (a TUI, a plugin) never breaks execution
                pass
        return item

    # ----------------------------------------------------------------- reading
    def snapshot(self) -> list[EventRecord]:
        with self._lock:
            return list(self._records)

    def since(self, seq: int) -> list[EventRecord]:
        return [r for r in self.snapshot() if r.seq > seq]

    def for_trace(self, trace_id: str) -> list[EventRecord]:
        return [r for r in self.snapshot() if r.trace_id == trace_id]

    def named(self, *prefixes: str) -> list[EventRecord]:
        return [r for r in self.snapshot() if r.name.startswith(prefixes)]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def records_from_log(lines: Iterable[dict[str, Any]]) -> list[EventRecord]:
    """Records rebuilt from the JSON Lines event log (``highhx events``), oldest first."""
    out: list[EventRecord] = []
    for seq, line in enumerate(lines, 1):
        envelope = {k: str(line.get(k, "")) for k in CONTEXT_KEYS}
        payload = {k: v for k, v in line.items() if k not in ("ts", "event", "session", *CONTEXT_KEYS)}
        if line.get("session") and not envelope["session_id"]:
            envelope["session_id"] = str(line["session"])
        out.append(EventRecord(seq, str(line.get("event", "")), str(line.get("ts", "")), payload, **envelope))
    return out
