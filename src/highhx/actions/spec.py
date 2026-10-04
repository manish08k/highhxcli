"""What an action is: a named, typed, risk-rated capability with a deterministic handler.

Every action in HighhX — typed as a command, resolved from plain language, used in a
workflow or proposed by the AI agent — is one of these and runs through the same
:class:`~highhx.actions.executor.ActionExecutor`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions.policy import Risk
from highhx.execution.retry import RetryPolicy
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Obj

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.tools.files import ChangeJournal
    from highhx.commands import App
    from highhx.computer.session import ComputerSession
    from highhx.execution.cancellation import CancellationToken
    from highhx.safety.actions import Actor

Inputs = dict[str, Any]

# Permissions an action needs (shown to the user, used for the agent's feature gating).
READ_PROJECT = "project.read"
WRITE_PROJECT = "project.write"
RUN_PROCESSES = "process.run"
GIT_LOCAL = "git.local"
GIT_REMOTE = "git.remote"
NETWORK = "network"
CONTAINERS = "containers"
DATABASE = "database"
DEPLOY = "deploy"
BROWSER = "browser"
DESKTOP = "desktop"
ANDROID = "android"
SANDBOX = "sandbox"


@dataclass
class ActionResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    changed: list[str] = field(default_factory=list)
    """Project-relative paths this action changed (for /changes and /undo)."""
    status: str = ""
    """ok · failed · denied · blocked · cancelled · timeout · planned · invalid"""
    verified: bool | None = None
    error: str = ""
    compensated: bool = False
    retryable: bool = True
    """False for deterministic failures (invalid input, confinement): retrying cannot help."""
    attempts: int = 1
    seconds: float = 0.0
    execution_id: str = ""
    """The history entry (``highhx history``) the action was recorded under."""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "ok" if self.ok else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "output": self.output,
            "changed": self.changed,
            "verified": self.verified,
            "error": self.error,
            "compensated": self.compensated,
            "attempts": self.attempts,
            "seconds": round(self.seconds, 3),
            **({"execution_id": self.execution_id} if self.execution_id else {}),
        }


@dataclass
class ActionContext:
    """What a handler may use. Side effects go through ``app.engine`` (commands) or the
    confinement helpers in :mod:`highhx.actions.handlers.files`."""

    app: App
    executor: ActionExecutor
    cancel: CancellationToken
    actor: Actor
    journal: ChangeJournal
    computer: Callable[[], ComputerSession]


Handler = Callable[[ActionContext, Inputs], ActionResult]
Verifier = Callable[[ActionContext, Inputs, ActionResult], tuple[bool, str]]
Compensator = Callable[[ActionContext, Inputs, ActionResult], str]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    """``category.verb``, e.g. ``git.push``."""
    description: str
    handler: Handler = field(repr=False, compare=False)
    inputs: Obj = field(default_factory=lambda: Obj({}), repr=False, compare=False)
    outputs: dict[str, str] = field(default_factory=dict)
    """Output fields and what they mean (the output schema)."""
    risk: Risk = Risk.SAFE
    """Floor: the effective risk is at least this (the classifier may raise it)."""
    kind: ActionKind = ActionKind.READ
    """How the safety classifier sees it."""
    permissions: tuple[str, ...] = ()
    timeout: float = 600.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    idempotent: bool = False
    """Only idempotent actions are ever retried automatically."""
    verify: Verifier | None = field(default=None, repr=False, compare=False)
    compensate: Compensator | None = field(default=None, repr=False, compare=False)
    """Undo a completed action when a later step of its workflow fails (rollback)."""
    feature: str | None = None
    """Plan feature the AI agent needs to use this action (Free users run it directly)."""
    agent: bool = True
    """Offered to the AI agent through `run_actions` (browser/desktop actions use the dedicated
    computer-use tools, which the platform checks by name)."""
    command: Callable[[Inputs], str | None] | None = field(default=None, repr=False, compare=False)
    """The concrete command this action runs (for classification and previews)."""
    target: Callable[[Inputs], str] | None = field(default=None, repr=False, compare=False)
    environment: Callable[[Inputs], str | None] | None = field(default=None, repr=False, compare=False)
    policy_action: Callable[[Inputs], str] | None = field(default=None, repr=False, compare=False)
    aliases: tuple[str, ...] = ()
    risk_for: Callable[[Inputs], Risk] | None = field(default=None, repr=False, compare=False)
    """A higher floor for some inputs (e.g. pressing Enter is riskier than pressing Escape)."""
    kind_for: Callable[[Inputs], ActionKind] | None = field(default=None, repr=False, compare=False)
    """How the classifier sees these inputs, when it depends on them (``browser.extract`` with a
    ``url`` navigates, and is classified as navigation: privileged schemes, sensitive URLs)."""
    preview: Callable[[App, Inputs], list[str]] | None = field(default=None, repr=False, compare=False)
    """What exactly will change, shown before approval (a diff for file writes …)."""

    @property
    def category(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def retries(self) -> RetryPolicy:
        return self.retry if self.idempotent else RetryPolicy()

    def validate(self, inputs: Inputs) -> list[str]:
        return self.inputs.validate(inputs, "")

    def command_for(self, inputs: Inputs) -> str | None:
        return self.command(inputs) if self.command else None

    def target_for(self, inputs: Inputs) -> str:
        return self.target(inputs) if self.target else ""

    def environment_for(self, inputs: Inputs) -> str | None:
        return self.environment(inputs) if self.environment else None

    def policy_name(self, inputs: Inputs) -> str:
        return self.policy_action(inputs) if self.policy_action else f"action:{self.name}"

    def describe(self, inputs: Inputs) -> str:
        """One line for previews: the action and its most telling input."""
        shown = self.command_for(inputs) or self.target_for(inputs)
        return f"{self.name}" + (f" · {shown}" if shown else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputs": self.inputs.json_schema(),
            "outputs": self.outputs,
            "risk": self.risk.label,
            "kind": str(self.kind),
            "permissions": list(self.permissions),
            "timeout": self.timeout,
            "retry": {"attempts": self.retries.attempts, "delay": self.retries.delay, "backoff": self.retries.backoff},
            "idempotent": self.idempotent,
            "verification": "yes" if self.verify else "no",
            "compensation": "yes" if self.compensate else "no",
            "feature": self.feature,
        }


def names(specs: Sequence[ActionSpec]) -> list[str]:
    return [s.name for s in specs]
