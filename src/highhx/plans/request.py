"""Run one plain-language request outside the interactive session: `highhx do …`,
`highhx "…"` and `highhx agent "…"` on HighhX Free when there is no terminal.

    request → deterministic decision → JSON plan → plan runner (executor: risk, approval, action,
    verification) → result lines or JSON → run trace

Returns the exit code, or None when the request is open-ended (the caller decides what the
Pro path is for it: an upsell, or the AI agent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.commands import App
    from highhx.decision.deterministic import Decision


def decide(app: App, text: str, executor: ActionExecutor | None = None) -> Decision:
    """The deterministic decision for ``text`` in this project (risk from the executor's classifier when given)."""
    from highhx.actions.resolver import ResolverContext
    from highhx.decision.deterministic import DeterministicDecider
    from highhx.plans.planner import catalog_risk

    if executor is None:
        return DeterministicDecider(ResolverContext.from_app(app)).decide(text)
    fallback = catalog_risk(executor.catalog)

    def risk_of(action: str, params: dict[str, Any]) -> Any:
        try:
            return executor.plan(action, params).decision.risk
        except HighhXError:
            return fallback(action, params)

    return DeterministicDecider(ResolverContext.from_app(app), catalog=executor.catalog, risk_of=risk_of).decide(text)


def run_request(app: App, text: str, *, source: str = "cli", plan_only: bool = False) -> int | None:
    from highhx.agent.ui import TerminalUI
    from highhx.observability.runs import RunTrace
    from highhx.plans.runner import PlanRunner, StepOutcome
    from highhx.plans.schema import PlanStep

    out = app.output
    executor = app.user_actions()
    decision = decide(app, text, executor)
    trace = RunTrace(decision, db=app.db, redactor=app.redactor, source=source)
    if plan_only:
        if app.options.json:
            out.json(decision.to_dict())
        else:
            _show_decision(app, decision)
        return 0 if decision.supported else 1
    if decision.plan is None:
        trace.finish()
        if decision.route == "pro":
            return None
        assert decision.unknown is not None
        if app.options.json:
            out.json({"ok": False, "run_id": trace.run_id, "decision": decision.to_dict()})
        else:
            out.error(decision.unknown.reason)
            for suggestion in decision.unknown.suggestions:
                out.console.print(f"  [dim]→ {suggestion}[/dim]")
            out.console.print("  [dim]Nothing ran.[/dim]")
        return 1
    console = out.err_console if app.options.json or app.options.quiet else out.console
    ui = TerminalUI(console, out.symbols, interactive=False)

    def execute(step: PlanStep) -> Any:
        try:
            planned = executor.plan(step.catalog_action, step.params)
        except HighhXError as exc:
            ui.notice("error", exc.message)
            return None
        ui.activity_started(step.description, detail=f"{step.catalog_action} · {planned.decision.risk.label}")
        result = executor.execute(planned, cancel=app.ctx.cancel)
        ui.action_finished(step.description, result)
        return result

    def verified(done: StepOutcome) -> None:
        if done.check is not None and done.action_ok:
            ui.step_verified(done.step.description, done.check.status, done.check.detail)

    outcome = PlanRunner(execute, root=app.root, on_verified=verified, trace=trace).run(decision.plan)
    if app.options.json:
        out.json(
            {"ok": outcome.ok, "run_id": trace.run_id, "decision": decision.to_dict(), "result": outcome.to_dict()}
        )
    elif not app.options.quiet:
        done = sum(1 for s in outcome.steps if s.ok)
        ui.print()
        ui.run_summary(trace.run_id, done, len(outcome.steps), outcome.verification, outcome.seconds, outcome.reason)
    return 0 if outcome.ok else 1


def _show_decision(app: App, decision: Decision) -> None:
    """`highhx do --plan`: the decision and plan, without running anything."""
    console = app.output.console
    console.print(f"[bold]{decision.normalized or decision.request}[/bold]")
    if decision.plan is None:
        why = decision.unknown.reason if decision.unknown else decision.reason
        console.print(f"  [dim]route[/dim] {decision.route} — {why}")
        return
    plan = decision.plan
    console.print(
        f"  [dim]route[/dim] local (deterministic) [dim]· intent[/dim] {plan.intent} [dim]· target[/dim] "
        f"{plan.target or '-'} [dim]· risk[/dim] {plan.risk} [dim]· executor[/dim] {plan.executor}"
    )
    for step in plan.steps:
        params = ", ".join(f"{k}={v}" for k, v in step.params.items())
        console.print(
            f"  {step.id}  {step.action:<7} {step.target:<14} [dim]{step.catalog_action}({params})[/dim] "
            f"{step.risk} [dim]· {step.executor} · verify {step.verification}[/dim]"
        )
    console.print("  [dim]Nothing ran (--plan). --json prints the JSON action plan.[/dim]")
