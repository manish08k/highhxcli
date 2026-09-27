"""Plans: the agent proposes one for multi-step work, the user approves it, and
step statuses drive the live progress feed (✓ Tests completed, ✗ 3 tests failing …)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult
from highhx.utils.validation import Int, List, Obj, Prop, Str

STEP_STATUSES = ("pending", "in_progress", "done", "failed", "skipped")


@dataclass
class PlanStep:
    title: str
    status: str = "pending"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "status": self.status, "note": self.note}


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    approved: bool = False

    @property
    def finished(self) -> bool:
        return all(s.status in ("done", "failed", "skipped") for s in self.steps)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STEP_STATUSES, 0)
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "approved": self.approved, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            str(data.get("goal") or ""),
            [
                PlanStep(str(s.get("title") or ""), str(s.get("status") or "pending"), str(s.get("note") or ""))
                for s in data.get("steps") or []
                if isinstance(s, dict)
            ],
            bool(data.get("approved")),
        )


class ProposePlanTool(Tool):
    name = "propose_plan"
    label = "Planning"
    untrusted_output = False
    description = """
Propose a step-by-step plan and ask the user to approve it before doing multi-step or
state-changing work (fixing bugs, refactoring, adding features, releases, deployments).
Inspect the project first so the plan is concrete. Skip this for simple questions or
single read-only lookups. The result tells you whether the user approved; if they
declined, revise the plan using their feedback or ask what they want instead.
"""
    schema = Obj(
        {
            "goal": Prop(Str(min_length=1), required=True, description="One sentence: what the plan achieves."),
            "steps": Prop(
                List(Str(min_length=1), min_items=1),
                required=True,
                description="3-10 short imperative steps, e.g. 'Run the test suite'.",
            ),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Plan: {args.get('goal', '')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        plan = Plan(str(args["goal"]), [PlanStep(str(s)) for s in args["steps"][:20]])
        approved, feedback = ctx.host.present_plan(plan)
        plan.approved = approved
        if approved:
            ctx.host.plan = plan
            return ToolResult(
                "The user approved the plan. Carry it out now. Call update_plan when a step starts "
                "(in_progress) and when it ends (done / failed / skipped, with a short note).",
                summary="Plan approved",
                data={"plan": plan.to_dict()},
            )
        message = "The user declined the plan."
        if feedback:
            message += f" Their feedback: {feedback}\nRevise the plan accordingly and propose it again."
        else:
            message += " Do not carry it out. Ask the user what they would like instead."
        return ToolResult(message, ok=False, summary="Plan declined")


class UpdatePlanTool(Tool):
    name = "update_plan"
    label = "Updating plan"
    untrusted_output = False
    description = """
Report progress on the approved plan: mark a step in_progress when you start it and
done / failed / skipped when it ends. Keep notes short and factual (e.g. '3 tests failing').
You may append a new step (index = number of steps) if the work reveals one.
"""
    schema = Obj(
        {
            "step": Prop(Int(minimum=1), required=True, description="1-based step number."),
            "status": Prop(Str(choices=STEP_STATUSES[1:]), required=True),
            "note": Prop(Str(), description="Short outcome, e.g. 'all 48 tests pass'."),
            "title": Prop(Str(), description="Only when adding a new step: its title."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Step {args.get('step')}: {args.get('status')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        plan = ctx.host.plan
        if plan is None:
            raise ToolError("There is no approved plan. Use propose_plan first (or just continue without one).")
        index = int(args["step"]) - 1
        if index == len(plan.steps) and args.get("title"):
            plan.steps.append(PlanStep(str(args["title"])))
        if not 0 <= index < len(plan.steps):
            raise ToolError(f"step must be between 1 and {len(plan.steps)}")
        step = plan.steps[index]
        step.status = str(args["status"])
        step.note = str(args.get("note") or "")
        ctx.host.plan_updated(plan, index)
        remaining = [i + 1 for i, s in enumerate(plan.steps) if s.status in ("pending", "in_progress")]
        return ToolResult(f"Step {index + 1} is now {step.status}. Remaining steps: {remaining or 'none'}.")
