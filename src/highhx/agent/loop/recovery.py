"""RecoveryManager: what to do when a step did not work. Bounded, risk-aware, audited.

    denied · blocked · cancelled        stop: a person or a policy said no, and that is final
    target not found                    (grounding already escalated through accessibility, DOM,
                                        text, OCR, vision and coordinates as the task allows)
                                        1. look again  2. scroll and look again  3. re-plan
    target ambiguous                    re-plan (be more specific), never pick one
    action failed, nothing ran          retry (a fresh request: classified and approved again)
    action failed, it may have run      SAFE / idempotent: retry
                                        anything riskier: never repeated silently, re-plan
                                        from a fresh observation instead
    verified FAILED / UNKNOWN           LOW risk: retry once. Otherwise re-plan

Every recovery has a maximum per step and in total, respects the task's deadline, re-evaluates
risk (each retry is a new ActionRequest the executor classifies again) and is emitted as
``recovery.started`` / ``recovery.completed`` (audited with the trace).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from highhx.actions.policy import Risk
from highhx.actions.protocol import ActionResponse, Outcome

FINAL_STATUSES = frozenset({"denied", "blocked", "cancelled"})
NOT_RUN_STATUSES = frozenset({"invalid", "planned"})


@dataclass(frozen=True)
class RecoveryAction:
    kind: str
    """retry · reobserve · scroll · replan · stop · ask_user"""
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason}


@dataclass
class RecoveryManager:
    max_per_step: int = 3
    max_total: int = 10
    deadline: float | None = None
    per_step: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def exhausted(self, step_id: str) -> str | None:
        if self.per_step.get(step_id, 0) >= self.max_per_step:
            return f"{self.max_per_step} recoveries of this step did not help"
        if self.total >= self.max_total:
            return f"the task used all {self.max_total} recoveries"
        if self.deadline is not None and time.monotonic() > self.deadline:
            return "the task ran out of time"
        return None

    def _take(self, step_id: str, action: RecoveryAction) -> RecoveryAction:
        if action.kind in ("retry", "reobserve", "scroll"):
            self.per_step[step_id] = self.per_step.get(step_id, 0) + 1
            self.total += 1
        return action

    def after_grounding_failure(self, step_id: str, status: str, attempts: list[dict[str, Any]]) -> RecoveryAction:
        """Grounding already escalated through every strategy the task allows (accessibility → DOM
        → text → OCR → vision → coordinates). What is left: the screen was still changing, or the
        target is off screen, or the plan is wrong."""
        if status == "ambiguous":
            return RecoveryAction("replan", "several elements match the target; the planner must be more specific")
        stop = self.exhausted(step_id)
        if stop:
            return RecoveryAction("replan", stop)
        ladder = [
            RecoveryAction("reobserve", "the screen may still be changing: look again"),
            RecoveryAction("scroll", "the target may be off screen: scroll and look again"),
        ]
        tried = self.per_step.get(step_id, 0)
        if tried < len(ladder):
            return self._take(step_id, ladder[tried])
        return RecoveryAction("replan", "the target could not be found on the screen")

    def after_action(self, step_id: str, response: ActionResponse | None, outcome: Outcome) -> RecoveryAction:
        if response is None:
            return RecoveryAction("replan", "nothing ran")
        status = response.result.status
        if status in FINAL_STATUSES:
            return RecoveryAction("stop", f"the action was {status}: {response.result.error or response.result.summary}")
        stop = self.exhausted(step_id)
        if stop:
            return RecoveryAction("replan", stop)
        if not response.result.ok:
            if response.result.retryable is False:
                return RecoveryAction("replan", f"retrying cannot help: {response.result.error}")
            if status == "timeout" and response.risk > Risk.SAFE:
                return RecoveryAction("replan", "it timed out and may have run; not repeating it silently")
            if response.risk <= Risk.SAFE or status in NOT_RUN_STATUSES:
                return self._take(step_id, RecoveryAction("retry", f"{status}: {response.result.error}"))
            return RecoveryAction("replan", f"it failed ({response.result.error}); not repeating a {response.risk.label}-risk action silently")
        if outcome == Outcome.UNKNOWN:
            return RecoveryAction("replan", "its effect could not be confirmed after observing again")
        if outcome == Outcome.FAILED:
            if response.risk <= Risk.LOW and self.per_step.get(step_id, 0) == 0:
                return self._take(step_id, RecoveryAction("retry", "the check failed; trying once more"))
            return RecoveryAction("replan", "it ran but the check failed")
        return RecoveryAction("replan", "partly done")
