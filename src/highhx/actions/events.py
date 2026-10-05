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
# The computer-use runtime (agent loop, perception, grounding, recovery, sandboxes).
# ``action.proposed`` is ACTION_PLANNED, ``action.retrying`` is ACTION_RETRY and
# ``approval.required`` is APPROVAL_REQUESTED (the existing names are kept for compatibility).
AGENT_PLANNING = "agent.planning"
PLAN_CREATED = "plan.created"
PLAN_UPDATED = "plan.updated"
OBSERVATION_CREATED = "observation.created"
GROUNDING_STARTED = "grounding.started"
GROUNDING_ATTEMPT = "grounding.attempt"
GROUNDING_COMPLETED = "grounding.completed"
VERIFICATION_STARTED = "verification.started"
VERIFICATION_COMPLETED = "verification.completed"
RECOVERY_STARTED = "recovery.started"
RECOVERY_COMPLETED = "recovery.completed"
AGENT_REFLECTION = "agent.reflection"
CHECKPOINT_CREATED = "checkpoint.created"
CHECKPOINT_RESUMED = "checkpoint.resumed"
TASK_STARTED = "task.started"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
TASK_PAUSED = "task.paused"
TASK_RESUMED = "task.resumed"
TASK_CANCELLED = "task.cancelled"
MODEL_REQUEST = "model.request"
MODEL_RESPONSE = "model.response"
MODEL_ERROR = "model.error"
VERIFICATION_FAILED = "verification.failed"
SELECTOR_HEALED = "selector.healed"
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"
SANDBOX_CREATED = "sandbox.created"
SANDBOX_EXEC = "sandbox.exec"
SANDBOX_DESTROYED = "sandbox.destroyed"
RUNTIME_STARTED = "runtime.started"
RUNTIME_STOPPED = "runtime.stopped"
MODEL_USAGE = "model.usage"
NETWORK_OBSERVED = "network.observed"
BENCHMARK_STARTED = "benchmark.started"
BENCHMARK_TASK = "benchmark.task"
BENCHMARK_COMPLETED = "benchmark.completed"

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
    AGENT_PLANNING,
    PLAN_CREATED,
    PLAN_UPDATED,
    OBSERVATION_CREATED,
    GROUNDING_STARTED,
    GROUNDING_ATTEMPT,
    GROUNDING_COMPLETED,
    VERIFICATION_STARTED,
    VERIFICATION_COMPLETED,
    RECOVERY_STARTED,
    RECOVERY_COMPLETED,
    AGENT_REFLECTION,
    CHECKPOINT_CREATED,
    CHECKPOINT_RESUMED,
    TASK_STARTED,
    TASK_COMPLETED,
    TASK_FAILED,
    VERIFICATION_FAILED,
    SELECTOR_HEALED,
    TOOL_STARTED,
    TOOL_COMPLETED,
    TOOL_FAILED,
    SANDBOX_CREATED,
    SANDBOX_EXEC,
    SANDBOX_DESTROYED,
    RUNTIME_STARTED,
    RUNTIME_STOPPED,
    MODEL_USAGE,
    NETWORK_OBSERVED,
    BENCHMARK_STARTED,
    BENCHMARK_TASK,
    BENCHMARK_COMPLETED,
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
    "plan.",
    "observation.",
    "grounding.",
    "verification.",
    "recovery.",
    "checkpoint.",
    "task.",
    "selector.",
    "tool.",
    "sandbox.",
    "runtime.",
    "model.",
    "network.",
    "benchmark.",
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
        for key, value in event.context.items():
            record.setdefault(key, value)
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


# ------------------------------------------------------------- canonical names
CANONICAL = {
    "agent.planning": "planning.started",
    "plan.created": "planning.completed",
    "approval.requested": "approval.required",
    "action.retry": "retry.started",
    "action.planned": "action.proposed",
    "computer.model.request": "model.request",
    "computer.model.response": "model.response",
    "sandbox.created": "sandbox.started",
    "sandbox.exec": "sandbox.completed",
}
"""Older names → the canonical vocabulary (docs/EVENTS.md). Both are the same event: records carry
``canonical`` so every consumer (web console, traces, exports) can filter on one vocabulary."""


def canonical(name: str) -> str:
    return CANONICAL.get(name, name)


BROWSER_JOURNAL = {
    "connected": "browser.started",
    "browser_gone": "browser.crash",
    "page_crashed": "browser.crash",
    "browser_hung": "browser.crash",
    "connection_lost": "browser.connection_lost",
    "no_answer": "browser.connection_lost",
    "recovered": "browser.recovered",
    "navigation_confirmed": "browser.navigation",
    "navigation_failed": "browser.navigation",
    "site_timeout": "browser.navigation",
    "tab_created": "browser.tab",
    "tab_switched": "browser.tab",
    "tab_closed": "browser.tab",
    "page_closed": "browser.tab",
    "download_started": "browser.download",
    "download_completed": "browser.download",
    "download_canceled": "browser.download",
}
"""What the browser went through (its journal), as bus events."""

INPUT_ACTIONS = frozenset(
    {
        "click",
        "click_at",
        "type",
        "press",
        "hotkey",
        "scroll",
        "drag",
        "move",
        "mouse_button",
        "edit",
        "menu",
        "window",
        "window_state",
        "focus",
        "launch",
        "quit",
    }
)
