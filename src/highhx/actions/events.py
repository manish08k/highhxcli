"""Structured execution events and their log.

Everything that runs emits events on the application's :class:`~highhx.core.events.EventBus`;
:class:`EventLog` appends them (secrets redacted) to a JSON Lines file per day, which
`highhx events` and `/history` read back.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.events import WILDCARD, Event, EventBus
from highhx.utils.paths import user_data_dir

if TYPE_CHECKING:
    from highhx.security.secrets import Redactor

SESSION_STARTED = "session.started"
SESSION_ENDED = "session.ended"
INTENT_RESOLVED = "intent.resolved"
INTENT_UNRESOLVED = "intent.unresolved"
ACTION_PLANNED = "action.planned"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_DENIED = "approval.denied"
ACTION_STARTED = "action.started"
ACTION_RETRY = "action.retry"
ACTION_COMPLETED = "action.completed"
ACTION_FAILED = "action.failed"
ACTION_COMPENSATED = "action.compensated"
WORKFLOW_STARTED = "workflow.started"
WORKFLOW_COMPLETED = "workflow.completed"
AGENT_STARTED = "agent.started"
AGENT_TURN = "agent.turn"
AGENT_TOOL_CALL = "agent.tool_call"
AGENT_COMPLETED = "agent.completed"
VOICE_HEARD = "voice.heard"
COMPUTER_TASK_STARTED = "computer.task.started"
COMPUTER_SCREENSHOT = "computer.screenshot"
COMPUTER_MODEL_REQUEST = "computer.model.request"
COMPUTER_MODEL_RESPONSE = "computer.model.response"
COMPUTER_ACTION_PREDICTED = "computer.action.predicted"
COMPUTER_ACTION_INVALID = "computer.action.invalid"
COMPUTER_ACTION_EXECUTED = "computer.action.executed"
COMPUTER_VERIFICATION_STARTED = "computer.verification.started"
COMPUTER_VERIFICATION_PASSED = "computer.verification.passed"
COMPUTER_VERIFICATION_FAILED = "computer.verification.failed"
COMPUTER_RECOVERY = "computer.recovery"
COMPUTER_TASK_COMPLETED = "computer.task.completed"
COMPUTER_TASK_FAILED = "computer.task.failed"
COMPUTER_TASK_NEEDS_USER = "computer.task.needs_user"
COMPUTER_TASK_CANCELLED = "computer.task.cancelled"
MCP_CONNECTED = "mcp.connected"
ATTACHMENTS_ADDED = "attachments.added"

EVENT_NAMES = (
    SESSION_STARTED,
    SESSION_ENDED,
    INTENT_RESOLVED,
    INTENT_UNRESOLVED,
    ACTION_PLANNED,
    APPROVAL_REQUESTED,
    APPROVAL_GRANTED,
    APPROVAL_DENIED,
    ACTION_STARTED,
    ACTION_RETRY,
    ACTION_COMPLETED,
    ACTION_FAILED,
    ACTION_COMPENSATED,
    WORKFLOW_STARTED,
    WORKFLOW_COMPLETED,
    AGENT_STARTED,
    AGENT_TURN,
    AGENT_TOOL_CALL,
    AGENT_COMPLETED,
    VOICE_HEARD,
    COMPUTER_TASK_STARTED,
    COMPUTER_SCREENSHOT,
    COMPUTER_MODEL_REQUEST,
    COMPUTER_MODEL_RESPONSE,
    COMPUTER_ACTION_PREDICTED,
    COMPUTER_ACTION_INVALID,
    COMPUTER_ACTION_EXECUTED,
    COMPUTER_VERIFICATION_STARTED,
    COMPUTER_VERIFICATION_PASSED,
    COMPUTER_VERIFICATION_FAILED,
    COMPUTER_RECOVERY,
    COMPUTER_TASK_COMPLETED,
    COMPUTER_TASK_FAILED,
    COMPUTER_TASK_NEEDS_USER,
    COMPUTER_TASK_CANCELLED,
    MCP_CONNECTED,
    ATTACHMENTS_ADDED,
)
LOGGED_PREFIXES = (
    "session.",
    "intent.",
    "action.",
    "approval.",
    "workflow.",
    "agent.",
    "voice.",
    "step.",
    "computer.",
    "mcp.",
    "attachments.",
)


def events_dir() -> Path:
    return user_data_dir() / "events"


class EventLog:
    """Appends bus events to ``events/YYYY-MM-DD.jsonl`` (redacted, one JSON object per line)."""

    def __init__(self, redactor: Redactor, *, directory: Path | None = None, session_id: str | None = None) -> None:
        self.redactor = redactor
        self.directory = directory or events_dir()
        self.session_id = session_id
        self._lock = threading.Lock()

    def attach(self, bus: EventBus) -> Callable[[], None]:
        return bus.subscribe(WILDCARD, self.write)

    def path_for(self, timestamp: str) -> Path:
        return self.directory / f"{timestamp[:10]}.jsonl"

    def write(self, event: Event) -> None:
        if not event.name.startswith(LOGGED_PREFIXES):
            return
        record: dict[str, Any] = {"ts": event.timestamp, "event": event.name, **_plain(event.data)}
        if self.session_id and "session" not in record:
            record["session"] = self.session_id
        line = self.redactor.redact(json.dumps(record, default=str, sort_keys=True))
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.path_for(event.timestamp).open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass  # the event log must never break execution


def _plain(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str | int | float | bool) or value is None:
            out[key] = value
        elif isinstance(value, list | tuple):
            out[key] = [v if isinstance(v, str | int | float | bool) else str(v) for v in value][:50]
        elif isinstance(value, dict):
            out[key] = {str(k): (v if isinstance(v, str | int | float | bool) else str(v)) for k, v in value.items()}
        else:
            out[key] = str(value)
    return out


def read_events(
    *, directory: Path | None = None, session: str | None = None, limit: int = 100, prefix: str | None = None
) -> list[dict[str, Any]]:
    """The most recent ``limit`` events (optionally of one session / with a name prefix), oldest first."""
    folder = directory or events_dir()
    found: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.jsonl"), reverse=True):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if session and record.get("session") != session:
                continue
            if prefix and not str(record.get("event", "")).startswith(prefix):
                continue
            found.append(record)
            if len(found) >= limit:
                return list(reversed(found))
    return list(reversed(found))


def iter_names() -> Iterator[str]:
    yield from EVENT_NAMES
