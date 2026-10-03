"""The agent loop's data: tasks, step intents, planner decisions, plans and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from highhx.core.events import new_id

if TYPE_CHECKING:
    from highhx.models.interfaces import ModelReply
    from highhx.trajectories.store import Trajectory

SURFACES = ("auto", "desktop", "browser", "android", "none")
VERBS = ("click", "double_click", "type", "press", "scroll", "open", "launch", "back", "home", "select", "wait")
"""Surface-neutral verbs a planner may use. The worker maps them to catalog actions for the surface."""


class Status(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_USER = "needs_user"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass
class AgentTask:
    goal: str
    surface: str = "auto"
    success: dict[str, Any] | list[Any] | None = None
    """Declarative verification of the whole task, checked on a fresh observation at the end."""
    max_steps: int = 30
    max_failures: int = 6
    max_replans: int = 3
    max_recoveries: int = 10
    timeout: float = 900.0
    allowed: tuple[str, ...] = ()
    """Allowed action prefixes (``browser.``, ``shell.run`` …); empty: every catalog action."""
    ocr: str = "auto"
    vision: str = "never"
    remote_vision: bool = False
    device: str = ""
    """Android device serial."""
    app: str = ""
    settle: float = 0.4
    """Seconds to wait after a UI action before observing its effect."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__) | {"allowed": list(self.allowed)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTask:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["allowed"] = tuple(known.get("allowed") or ())
        return cls(**known)

    def permits(self, action: str) -> bool:
        return not self.allowed or any(action == a or action.startswith(a) for a in self.allowed)


@dataclass
class StepIntent:
    action: str
    """A verb (``click``, ``type`` …) or a catalog action name (``shell.run``, ``android.launch`` …)."""
    target: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    verify: dict[str, Any] | list[Any] | None = None
    surface: str | None = None
    id: str = field(default_factory=lambda: new_id("step"))

    @property
    def label(self) -> str:
        return str(self.target.get("label") or "")

    @property
    def is_verb(self) -> bool:
        return self.action in VERBS

    def describe(self) -> str:
        what = self.intent or f"{self.action} {self.label}".strip()
        return what

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "parameters": self.parameters,
            "intent": self.intent,
            "verify": self.verify,
            "surface": self.surface,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepIntent:
        target = data.get("target") or {}
        if isinstance(target, str):
            target = {"label": target}
        return cls(
            action=str(data.get("action") or ""),
            target=dict(target),
            parameters=dict(data.get("parameters") or data.get("params") or {}),
            intent=str(data.get("intent") or ""),
            verify=data.get("verify") or data.get("verification"),
            surface=data.get("surface"),
            id=str(data.get("id") or new_id("step")),
        )


@dataclass
class Decision:
    kind: str
    """act · done · ask_user · fail"""
    step: StepIntent | None = None
    summary: str = ""
    thought: str = ""
    reply: ModelReply | None = None


@dataclass
class PlanItem:
    title: str
    status: str = "pending"
    """pending · running · done · failed · skipped"""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "status": self.status}


@dataclass
class LoopResult:
    status: Status
    summary: str
    trajectory: Trajectory
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == Status.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "summary": self.summary,
            "task_id": self.trajectory.id,
            "trace_id": self.trajectory.trace_id,
            "steps": len(self.trajectory.steps),
            "metrics": self.metrics,
        }
