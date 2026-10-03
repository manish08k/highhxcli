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
        (out.success if result.ok else (out.warn if result.status == "needs_user" else out.error))(f"{result.status}: {result.summary}")
        m = result.metrics
        out.note(f"  {len(result.trajectory.steps)} step(s) · {m.get('actions', 0)} action(s) · {m.get('recoveries', 0)} recovery · {m.get('seconds', 0)}s")
        out.note(f"  trace: highhx trace show {result.trajectory.trace_id}")
        if result.status in ("interrupted", "needs_user", "failed"):
            out.note(f"  resume: highhx agent --resume {result.trajectory.id}")

    out.emit(result.to_dict(), render)
    return {"completed": 0, "needs_user": 3, "interrupted": 130, "cancelled": 130}.get(str(result.status), 1)


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
        planner = computer_use.planner_for(app, handle.executor, goal, plan=Path(plan_file) if plan_file else None, model=model, remote_model=remote_model)
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
@click.option("--surface", type=click.Choice(["auto", "browser", "desktop", "android", "none"]), default="auto", show_default=True)
@click.option("--plan", "plan_file", type=click.Path(exists=True, dir_okay=False), help="Steps from a YAML/JSON file (no AI).")
@click.option("--model", is_flag=True, help="Plan with a model (HighhX Pro or a local model).")
@click.option("--remote-model", is_flag=True, help="Allow a remote model to see the task and screen.")
@click.option("--vision", is_flag=True, help="Allow a vision model for grounding when structure and OCR fail.")
@click.option("--success", metavar="JSON", help='The task\'s success check, e.g. \'{"text": "Order placed"}\'.')
@click.option("--max-steps", type=click.IntRange(1, 500), default=30, show_default=True)
@click.option("--resume", "resume_id", metavar="TASK_ID", help="Continue an interrupted task from its checkpoint.")
@click.option("--live/--no-live", default=True, help="Show the live dashboard.")
@click.option("--device", metavar="SERIAL", help="Android device.")
@pass_app
def agent_loop(app: App, goal: str | None, surface: str, plan_file: str | None, model: bool, remote_model: bool, vision: bool, success: str | None, max_steps: int, resume_id: str | None, live: bool, device: str | None) -> int:
    """The computer-use agent loop (Planner → Worker → Observer → Verifier → Reflector): every
    action goes through HighhX's executor — risk, policy, approval on this terminal, verification,
    audit — and the run is recorded as a trajectory and a task trace. Steps come from --plan, a
    model (--model), or HighhX Free's deterministic resolver."""
    if resume_id:
        return resume_goal(app, resume_id, live=live, model=model, remote_model=remote_model)
    if not goal:
        raise click.UsageError("Give a GOAL, or --resume TASK_ID.")
    return run_goal(app, goal, surface=surface, plan_file=plan_file, model=model, remote_model=remote_model, max_steps=max_steps, live=live, device=device or "", success=success, vision=vision)
