"""What a running task remembers: the goal, where it is, and everything that happened.

The loop records each step (the action, its result, its verification, the page before and
after) so the planner and the recovery policy decide from facts — which actions already
failed on which page, how many recoveries were spent, which tab the task works in.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from highhx.computer.model import Observation
from highhx.goals.ir import Action, TaskIR

COMPLETED, FAILED, CANCELLED, NEEDS_USER, RUNNING = "completed", "failed", "cancelled", "needs_user", "running"


def page_key(observation: Observation | None) -> str:
    """A short hash of what the page shows — the same action on the same page is the same attempt."""
    if observation is None:
        return "-"
    return hashlib.sha256(repr(observation.fingerprint()).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class PageSummary:
    url: str
    title: str
    tab: str = ""

    @classmethod
    def of(cls, observation: Observation | None, tab: str = "") -> PageSummary:
        if observation is None:
            return cls("", "", tab)
        return cls(observation.url, observation.title, tab)

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "title": self.title, "tab": self.tab}


@dataclass
class StepRecord:
    number: int
    action: Action
    ok: bool
    verified: bool | None
    summary: str
    problems: list[str] = field(default_factory=list)
    before: PageSummary | None = None
    after: PageSummary | None = None
    output: dict[str, Any] = field(default_factory=dict)
    recovery: str = ""
    """How the loop recovered from this step's failure (empty when it succeeded)."""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.number,
            "action": self.action.to_dict(),
            "ok": self.ok,
            "verified": self.verified,
            "summary": self.summary,
            "problems": self.problems,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "output": self.output,
            "recovery": self.recovery,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class TaskState:
    task: TaskIR
    id: str = field(default_factory=lambda: f"task_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:4]}")
    status: str = RUNNING
    reason: str = ""
    started: float = field(default_factory=time.monotonic)
    steps: list[StepRecord] = field(default_factory=list)
    observations: list[PageSummary] = field(default_factory=list)
    """Every page the task observed, in order (bounded)."""
    last_observation: Observation | None = None
    failed: dict[str, str] = field(default_factory=dict)
    """signature@page → why it failed: never issued again on that same page."""
    attempts: dict[str, int] = field(default_factory=dict)
    """signature → how often it ran (on any page): bounds retries of the same action."""
    failures: int = 0
    recoveries: int = 0
    replans: int = 0
    invalid_proposals: int = 0
    extracted: dict[str, Any] = field(default_factory=dict)
    """What ``read`` found (text or regular-expression matches), for later steps and the result."""
    tab: str = ""
    planner: str = ""

    MAX_OBSERVATIONS = 200

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def done(self) -> bool:
        return self.status != RUNNING

    def observed(self, observation: Observation, tab: str = "") -> None:
        self.last_observation = observation
        self.tab = tab or self.tab
        self.observations.append(PageSummary.of(observation, self.tab))
        del self.observations[: -self.MAX_OBSERVATIONS]

    def key(self, action: Action, observation: Observation | None = None) -> str:
        return f"{action.signature()}@{page_key(observation or self.last_observation)}"

    def already_failed(self, action: Action) -> str | None:
        return self.failed.get(self.key(action))

    def finish(self, status: str, reason: str = "") -> None:
        self.status, self.reason = status, reason

    def history(self, limit: int = 12) -> list[dict[str, Any]]:
        """The recent steps, compactly — what a planner needs to decide the next action."""
        out = []
        for record in self.steps[-limit:]:
            item: dict[str, Any] = {"action": record.action.call(), "ok": record.ok, "verified": record.verified}
            if record.problems:
                item["problems"] = record.problems[:3]
            if record.after:
                item["page"] = record.after.url
            if record.recovery:
                item["recovery"] = record.recovery
            out.append(item)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.task.goal,
            "status": self.status,
            "reason": self.reason,
            "planner": self.planner,
            "seconds": round(self.elapsed, 3),
            "steps": [s.to_dict() for s in self.steps],
            "failures": self.failures,
            "recoveries": self.recoveries,
            "replans": self.replans,
            "extracted": self.extracted,
            "tab": self.tab,
            "task": self.task.to_dict(),
        }
