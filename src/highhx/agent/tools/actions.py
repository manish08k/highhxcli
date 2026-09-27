"""run_actions: the AI agent proposes a structured action graph; HighhX executes it.

The model never runs anything itself. It names catalog actions and their inputs; HighhX
validates the whole graph before the first step (known actions, valid inputs, known
dependencies, no cycles, allowed by the account's plan), then each step goes through the
same policy, approval (the agent can never pre-approve), execution, verification and audit
as a Free user's action. Browser and desktop actions are not offered here — the agent uses
the dedicated computer-use tools, which the platform entitles by name.
"""

from __future__ import annotations

import json
from typing import Any

from highhx.actions.catalog import default_catalog
from highhx.actions.executor import ActionExecutor, ActionNode, GraphError, run_graph
from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate
from highhx.core.errors import HighhXError
from highhx.safety.actions import Actor
from highhx.utils.validation import Any_, Bool, List, Map, Obj, Prop, Str

MAX_ACTIONS = 25


class RunActionsTool(Tool):
    name = "run_actions"
    label = "Running actions"
    untrusted_output = True
    timeout = 24 * 3600.0

    def __init__(self, features: frozenset[str]) -> None:
        self.features = features
        self.allowed = {s.name: s for s in default_catalog().for_agent(features)}
        catalog = "\n".join(f"- {s.name} [{s.risk.label}]: {s.description}" for s in self.allowed.values())
        self.description = f"""
Run a structured graph of HighhX actions (deterministic, policy-checked, approval-gated,
audited). Use it for multi-step work with the project: each item is an action with inputs;
`depends_on` orders items; a failing item stops the items after it. With
rollback_on_failure, completed items that can be undone are undone when a later one fails.
Risk decides approval (the user approves medium/high risk, typed confirmation for critical)
— HighhX decides risk, not you. Inspect inputs with the action names below.

Actions:
{catalog}
"""
        self.schema = Obj(
            {
                "actions": Prop(
                    List(
                        Obj(
                            {
                                "id": Prop(
                                    Str(min_length=1), description="Unique id for depends_on (default: step N)."
                                ),
                                "action": Prop(Str(min_length=1), required=True),
                                "inputs": Prop(Map(Any_())),
                                "depends_on": Prop(List(Str(min_length=1))),
                                "continue_on_error": Prop(Bool()),
                            }
                        ),
                        min_items=1,
                    ),
                    required=True,
                ),
                "rollback_on_failure": Prop(Bool()),
            }
        )

    def describe(self, args: dict[str, Any]) -> str:
        items = args.get("actions") or []
        names = [str(i.get("action")) for i in items if isinstance(i, dict)]
        shown = " → ".join(names[:4]) + (" …" if len(names) > 4 else "")
        return f"Run {len(names)} action{'s' if len(names) != 1 else ''}: {shown}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        items = list(args["actions"])
        if len(items) > MAX_ACTIONS:
            raise ToolError(f"at most {MAX_ACTIONS} actions per call — split the work")
        nodes: list[ActionNode] = []
        for index, item in enumerate(items, start=1):
            name = str(item["action"])
            if name not in self.allowed:
                spec = default_catalog().get(name)
                if spec is None:
                    raise ToolError(f"unknown action '{name}' (see the list in this tool's description)")
                if not spec.agent:
                    raise ToolError(f"'{name}' is not available here — use the computer-use tools for UI actions")
                raise ToolError(f"'{name}' is not included in the account's plan")
            nodes.append(
                ActionNode(
                    id=str(item.get("id") or f"step{index}"),
                    action=name,
                    inputs=dict(item.get("inputs") or {}),
                    depends_on=tuple(str(d) for d in item.get("depends_on") or []),
                    continue_on_error=bool(item.get("continue_on_error")),
                )
            )
        executor = ActionExecutor(
            ctx.app,
            ctx.permissions.gate,
            actor=Actor.AGENT,
            journal=ctx.journal,
            computer=ctx.computer,
        )
        ui = getattr(ctx.host, "ui", None)

        def shown(node: ActionNode, result: Any) -> None:
            if ui is not None and hasattr(ui, "action_finished"):
                ui.action_finished(f"{node.action}", result)

        try:
            outcome = run_graph(
                executor,
                nodes,
                cancel=ctx.cancel,
                rollback=bool(args.get("rollback_on_failure")),
                on_result=shown,
            )
        except GraphError as exc:
            raise ToolError(exc.message) from None
        except HighhXError as exc:
            detail = "; ".join(exc.details[:5])
            raise ToolError(exc.message + (f": {detail}" if detail else "")) from None
        data = outcome.to_dict()
        done = sum(1 for r in outcome.results.values() if r.ok)
        changed = [path for r in outcome.results.values() for path in r.changed]
        denied = [n for n, r in outcome.results.items() if r.status in ("denied", "blocked")]
        summary = f"{done}/{len(nodes)} actions succeeded"
        if denied:
            summary += f" · not approved/blocked: {', '.join(denied)}"
        if outcome.compensations:
            summary += f" · rolled back {len(outcome.compensations)}"
        return ToolResult(
            truncate(json.dumps(data, indent=1, default=str)),
            ok=outcome.ok,
            summary=summary,
            data=data,
            changed_files=changed,
            error_code=None if outcome.ok else ("denied" if denied else "failed"),
            verified=all(r.verified is not False for r in outcome.results.values()),
        )
