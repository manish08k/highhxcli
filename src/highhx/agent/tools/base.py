"""Tool framework: what the agent can do, expressed through HighhX's own services.

Each tool declares an input schema (validated before it runs — model output is
untrusted), a base risk, the plan feature it needs, and how to describe a call
to the user. Side effects go through the HighhX engine (policy → risk →
approval → execute → history), never around it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from highhx.agent.model.base import ToolSpec
from highhx.approvals.risk import RiskLevel
from highhx.utils.validation import Obj

if TYPE_CHECKING:
    from highhx.agent.permissions import AgentPermissions
    from highhx.agent.planner import Plan
    from highhx.agent.tools.files import ChangeJournal
    from highhx.commands import App
    from highhx.computer.session import ComputerSession
    from highhx.execution.cancellation import CancellationToken

MAX_RESULT_CHARS = 30_000
"""Longest tool output returned to the model (head and tail are kept)."""


class ToolError(Exception):
    """A tool could not do what was asked; the message goes back to the model."""


@dataclass
class ToolResult:
    content: str
    """What the model sees."""
    ok: bool = True
    summary: str = ""
    """One line for the user, e.g. ``12 passed, 3 failed``."""
    data: dict[str, Any] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    error_code: str | None = None
    """Structured failure class: invalid_input, unknown_tool, denied, policy, not_found, timeout,
    cancelled, failed, internal."""
    verified: bool | None = None
    """Whether the outcome was checked (file re-read, exit code, UI re-observed …); None = not applicable."""

    @classmethod
    def json(cls, data: Any, *, summary: str = "", ok: bool = True) -> ToolResult:
        return cls(json.dumps(data, indent=1, default=str), ok=ok, summary=summary, error_code=None if ok else "failed")

    @classmethod
    def error(cls, message: str, *, summary: str = "", code: str = "failed") -> ToolResult:
        first = message.strip().splitlines()[0][:120] if message.strip() else code
        return cls(message, ok=False, summary=summary or first, error_code=code)


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Keep the head and tail of long output (test failures are usually at the end)."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n… [{omitted} characters omitted] …\n\n{text[-tail:]}"


class PlanHost(Protocol):
    """What the plan tools need from the session."""

    plan: Plan | None

    def present_plan(self, plan: Plan) -> tuple[bool, str]:
        """Show a proposed plan; returns (approved, feedback)."""
        ...

    def plan_updated(self, plan: Plan, index: int) -> None: ...


@dataclass
class ToolContext:
    app: App
    permissions: AgentPermissions
    cancel: CancellationToken
    host: PlanHost
    remember: Callable[[str], str]
    journal: ChangeJournal
    computer: Callable[[], ComputerSession]
    """The session's computer-use context (created on first use)."""
    output_lines: list[str] = field(default_factory=list)
    """Command output captured while the current tool runs (fed by the engine sink)."""


class Tool:
    """Base class. Subclasses set the class attributes and implement :meth:`run`."""

    name: str = ""
    label: str = ""
    """Progress label shown while running, e.g. ``Running tests``."""
    description: str = ""
    schema: Obj = Obj({})
    risk: RiskLevel = RiskLevel.SAFE
    mutating: bool = False
    feature: str | None = None
    """Plan feature required (``None`` = included with the agent)."""
    requires_project: bool = False
    """Needs an initialized HighhX project (`highhx init`)."""
    timeout: float = 1800.0
    """Seconds before the tool is cancelled (commands also have their own timeouts)."""
    untrusted_output: bool = True
    """Output contains external data (files, command output, web pages) and is framed as untrusted."""

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description.strip(), self.schema.json_schema())

    def validate(self, args: dict[str, Any]) -> list[str]:
        if "INVALID_JSON" in args:
            return ["the tool input was not valid JSON — send the call again with a well-formed JSON object"]
        return self.schema.validate(args, "")

    def describe(self, args: dict[str, Any]) -> str:
        """Short description of one call for the activity feed."""
        return self.label or self.name

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise NotImplementedError
