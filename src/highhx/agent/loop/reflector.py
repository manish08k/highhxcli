"""AgentReflector: after each step, decide continue · retry · re-plan · stop · ask the person,
and write down why (the reason goes into the trajectory and back to the planner)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from highhx.actions.protocol import ActionResponse, Outcome
from highhx.agent.loop.recovery import RecoveryAction, RecoveryManager


@dataclass(frozen=True)
class Reflection:
    decision: str
    """continue · retry · reobserve · scroll · replan · stop · ask_user"""
    reason: str
    lesson: str = ""
    recovery: RecoveryAction | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "lesson": self.lesson,
            "recovery": self.recovery.to_dict() if self.recovery else None,
        }


class AgentReflector:
    def __init__(self, recovery: RecoveryManager) -> None:
        self.recovery = recovery

    def after_step(self, step_id: str, response: ActionResponse | None, outcome: Outcome, label: str = "") -> Reflection:
        if outcome == Outcome.SUCCESS:
            return Reflection("continue", "verified")
        action = self.recovery.after_action(step_id, response, outcome)
        lesson = ""
        if response is not None and response.result.error:
            lesson = f"{response.action} {label!r}: {response.result.error}".strip()
        if outcome == Outcome.PARTIAL_SUCCESS and action.kind == "replan":
            return Reflection("replan", "only part of the expected result is there", lesson, action)
        return Reflection(action.kind, action.reason, lesson, action)

    def after_grounding(self, step_id: str, status: str, attempts: list[dict[str, Any]], label: str) -> Reflection:
        action = self.recovery.after_grounding_failure(step_id, status, attempts)
        tried = ", ".join(f"{a.get('strategy')} {a.get('result')}" for a in attempts)
        return Reflection(action.kind, action.reason, f"{label!r} not grounded ({tried})", action)
