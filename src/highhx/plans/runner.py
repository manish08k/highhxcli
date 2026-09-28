"""Execute an action plan: step by step, verified, stopping safely at the first failure.

    for each step:   execute (the caller's executor: classification, approval, the action)
                     verify  (the step's strategy — :mod:`highhx.verification.strategies`)
                     a step that "succeeded" but failed verification is a failed step
    on failure:      stop; later steps are reported as skipped, never as done

The runner knows nothing about terminals or JSON output: the caller passes how to execute
one step (the REPL shows live activity and asks for approvals; ``highhx do`` prints lines)
and optional callbacks. It records everything in a :class:`RunTrace` when given one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.plans.schema import ActionPlan, PlanStep
from highhx.verification.strategies import FAILED, UNVERIFIED, Check, Evidence, overall, verify

if TYPE_CHECKING:
    from highhx.actions.spec import ActionResult
    from highhx.observability.runs import RunTrace

Execute = Callable[[PlanStep], "ActionResult | None"]


@dataclass
class StepOutcome:
    step: PlanStep
    status: str
    """succeeded, failed, denied, blocked, cancelled or skipped"""
    check: Check | None = None
    error: str = ""
    seconds: float = 0.0
    summary: str = ""
    action_ok: bool = False
    """The action itself reported success (its verification may still have failed)."""

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step.id,
            "action": self.step.action,
            "catalog_action": self.step.catalog_action,
            "target": self.step.target,
            "status": self.status,
            "verification": self.check.to_dict() if self.check else None,
            "error": self.error,
            "summary": self.summary,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class PlanOutcome:
    plan: ActionPlan
    steps: list[StepOutcome] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def status(self) -> str:
        if self.ok:
            return "succeeded"
        return "cancelled" if any(s.status == "cancelled" for s in self.steps) else "failed"

    @property
    def failed_step(self) -> StepOutcome | None:
        return next((s for s in self.steps if s.status not in ("succeeded", "skipped")), None)

    @property
    def verification(self) -> str:
        return overall([s.check for s in self.steps if s.check is not None])

    @property
    def reason(self) -> str:
        failed = self.failed_step
        return f"{failed.step.id} ({failed.step.description}): {failed.error or failed.status}" if failed else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "verification": self.verification,
            "failed_step": self.failed_step.step.id if self.failed_step else None,
            "reason": self.reason,
            "seconds": round(self.seconds, 3),
            "steps": [s.to_dict() for s in self.steps],
        }


class PlanRunner:
    def __init__(
        self,
        execute: Execute,
        *,
        root: Path | None,
        on_verified: Callable[[StepOutcome], None] | None = None,
        trace: RunTrace | None = None,
    ) -> None:
        self.execute = execute
        self.root = root
        self.on_verified = on_verified
        self.trace = trace

    def run(self, plan: ActionPlan) -> PlanOutcome:
        outcome = PlanOutcome(plan)
        began = time.monotonic()
        stopped = False
        for step in plan.steps:
            if stopped:
                outcome.steps.append(StepOutcome(step, "skipped", error="an earlier step did not succeed"))
                continue
            started = time.monotonic()
            result = self.execute(step)
            seconds = time.monotonic() - started
            done = self._judge(step, result, seconds)
            outcome.steps.append(done)
            if self.on_verified is not None and result is not None:
                self.on_verified(done)
            if not done.ok:
                stopped = True
        outcome.seconds = time.monotonic() - began
        if self.trace is not None:
            self.trace.finish(outcome)
        return outcome

    def _judge(self, step: PlanStep, result: ActionResult | None, seconds: float) -> StepOutcome:
        if result is None:
            return StepOutcome(step, "failed", error="the action could not be planned", seconds=seconds)
        if not result.ok:
            status = result.status if result.status in ("denied", "blocked", "cancelled", "timeout") else "failed"
            check = Check(FAILED, result.error or status)
            return StepOutcome(step, status, check, result.error or status, seconds, result.summary)
        if result.status == "planned":  # --dry-run: previewed, never executed — nothing to verify
            check = Check(UNVERIFIED, "dry run — nothing was executed")
            return StepOutcome(step, "succeeded", check, "", seconds, result.summary, action_ok=True)
        check = verify(
            step.verification,
            Evidence(
                step.catalog_action,
                dict(step.params),
                True,
                result.verified,
                dict(result.output),
                result.error,
                self.root,
            ),
        )
        if check.failed:
            return StepOutcome(
                step, "failed", check, f"verification failed: {check.detail}", seconds, result.summary, action_ok=True
            )
        return StepOutcome(step, "succeeded", check, "", seconds, result.summary, action_ok=True)
