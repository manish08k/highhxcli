"""highhx agent loop — the computer-use agent loop from the command line (shared by android agent)."""

from __future__ import annotations

from pathlib import Path

import click

from highhx.commands import App, pass_app


def _report(app: App, result: object) -> int:
    from highhx.agent.loop import LoopResult

    assert isinstance(result, LoopResult)
    out = app.output

    def render() -> None:
        if result.status == "planned":  # a dry run: the plan, its risks and approvals — never "completed"
            head, *steps = result.summary.split("\n")
            out.warn(head)
            for line in steps:
                out.plain(f"  {line}")
        else:
            (out.success if result.ok else (out.warn if result.status == "needs_user" else out.error))(
                f"{result.status}: {result.summary}"
            )
        m = result.metrics
        out.note(
            f"  {len(result.trajectory.steps)} step(s) · {m.get('actions', 0)} action(s) · {m.get('recoveries', 0)} recovery · {m.get('seconds', 0)}s"
        )
        out.note(f"  trace: highhx trace show {result.trajectory.trace_id}")
        if result.status in ("interrupted", "needs_user", "failed"):
            out.note(f"  resume: highhx agent --resume {result.trajectory.id}")

    out.emit(result.to_dict(), render)
    return {"completed": 0, "planned": 0, "needs_user": 3, "interrupted": 130, "cancelled": 130}.get(
        str(result.status), 1
    )


def run_goal(
    app: App,
    goal: str,
    *,
    surface: str = "auto",
    plan_file: str | None = None,
    model: bool = False,
    remote_model: bool = False,
    max_steps: int = 30,
    live: bool = True,
    device: str = "",
    success: str | None = None,
    vision: bool = False,
) -> int:
    import json

    from highhx import computer_use
    from highhx.agent.loop import AgentTask

    task = AgentTask(
        goal,
        surface=surface,
        max_steps=max_steps,
        device=device,
        success=json.loads(success) if success else None,
        vision="auto" if vision else "never",
        remote_vision=remote_model,
    )
    with computer_use.session(app, agent=model, live=live and app.output.human, source="agent-loop") as handle:
        planner = computer_use.planner_for(
            app,
            handle.executor,
            goal,
            plan=Path(plan_file) if plan_file else None,
            model=model,
            remote_model=remote_model,
        )
        result = computer_use.run_task(handle.executor, planner, task)
    return _report(app, result)


def resume_goal(app: App, task_id: str, *, live: bool = True, model: bool = False, remote_model: bool = False) -> int:
    from highhx import computer_use

    with computer_use.session(app, agent=model, live=live and app.output.human, source="agent-loop") as handle:
        planner = None
        if model:
            from highhx.agent.loop import ModelPlanner
            from highhx.models.registry import language_model

            planner = ModelPlanner(language_model(app, app.ctx.cancel, remote_ok=remote_model), handle.executor.catalog)
        result = computer_use.resume_task(handle.executor, task_id, planner=planner)
    return _report(app, result)


@click.command("loop", short_help="Work toward a goal: plan, act, observe, verify, recover.")
@click.argument("goal", required=False)
@click.option(
    "--surface", type=click.Choice(["auto", "browser", "desktop", "android", "none"]), default="auto", show_default=True
)
@click.option(
    "--plan", "plan_file", type=click.Path(exists=True, dir_okay=False), help="Steps from a YAML/JSON file (no AI)."
)
@click.option("--model", is_flag=True, help="Plan with a model (HighhX Pro or a local model).")
@click.option("--remote-model", is_flag=True, help="Allow a remote model to see the task and screen.")
@click.option("--vision", is_flag=True, help="Allow a vision model for grounding when structure and OCR fail.")
@click.option("--success", metavar="JSON", help='The task\'s success check, e.g. \'{"text": "Order placed"}\'.')
@click.option("--max-steps", type=click.IntRange(1, 500), default=30, show_default=True)
@click.option("--resume", "resume_id", metavar="TASK_ID", help="Continue an interrupted task from its checkpoint.")
@click.option("--live/--no-live", default=True, help="Show the live dashboard.")
@click.option("--device", metavar="SERIAL", help="Android device.")
@click.option(
    "--best-of",
    "best_of",
    type=click.IntRange(2, 10),
    help="With --model and --surface none: N independent attempts on project copies; keep the best (its diff is shown, not applied).",
)
@pass_app
def agent_loop(
    app: App,
    goal: str | None,
    surface: str,
    plan_file: str | None,
    model: bool,
    remote_model: bool,
    vision: bool,
    success: str | None,
    max_steps: int,
    resume_id: str | None,
    live: bool,
    device: str | None,
    best_of: int | None,
) -> int:
    """The computer-use agent loop (Planner → Worker → Observer → Verifier → Reflector): every
    action goes through HighhX's executor — risk, policy, approval on this terminal, verification,
    audit — and the run is recorded as a trajectory and a task trace. Steps come from --plan, a
    model (--model), or HighhX Free's deterministic resolver."""
    if resume_id:
        return resume_goal(app, resume_id, live=live, model=model, remote_model=remote_model)
    if not goal:
        raise click.UsageError("Give a GOAL, or --resume TASK_ID.")
    if best_of:
        return best_of_goal(
            app,
            goal,
            best_of,
            success=success,
            max_steps=max_steps,
            remote_model=remote_model,
            model=model,
            surface=surface,
        )
    return run_goal(
        app,
        goal,
        surface=surface,
        plan_file=plan_file,
        model=model,
        remote_model=remote_model,
        max_steps=max_steps,
        live=live,
        device=device or "",
        success=success,
        vision=vision,
    )


def best_of_goal(
    app: App,
    goal: str,
    attempts: int,
    *,
    success: str | None,
    max_steps: int,
    remote_model: bool,
    model: bool,
    surface: str,
) -> int:
    """Best-of-N on project copies: only for model-planned tasks without a screen (a real screen
    cannot be rolled back between attempts). Approvals are asked on this terminal, per attempt."""
    import json

    from highhx.agent.loop import AgentTask, ModelPlanner
    from highhx.agent.loop.best_of import BestOfN, project_copies
    from highhx.agent.ui import TerminalUI
    from highhx.models.registry import language_model

    if not model or surface != "none":
        raise click.UsageError("--best-of needs --model and --surface none (attempts run on copies of the project).")
    llm = language_model(app, app.ctx.cancel, remote_ok=remote_model)
    ui = TerminalUI(app.output.console, app.output.symbols, interactive=app.options.is_interactive())
    copies = project_copies(app, approvals=ui)

    catalog = _catalog(app)

    def planner_for(number: int) -> ModelPlanner:
        return ModelPlanner(llm, catalog)

    task = AgentTask(goal, surface="none", success=json.loads(success) if success else None, max_steps=max_steps)
    result = BestOfN(copies, planner_for, attempts=attempts).run(task)
    out = app.output

    def render() -> None:
        for attempt in result.attempts:
            out.plain(
                f"  attempt {attempt.number}: {attempt.status}  score {attempt.score}  {attempt.steps} step(s)  {attempt.task_id}"
            )
        if result.best is not None:
            out.success(f"best: attempt {result.best.number} (score {result.best.score}); stopped: {result.stopped}")
            if result.diff:
                out.plain(result.diff)
                out.note("Not applied. Review it, then apply it yourself (e.g. save it and `git apply`).")

    out.emit(result.to_dict(), render)
    return 0 if result.best is not None and result.best.score > 0 else 1


def _catalog(app: App):  # type: ignore[no-untyped-def]
    from highhx.actions.catalog import catalog_for

    return catalog_for(app)
