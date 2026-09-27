"""highhx workflow run / runs / inspect / resume / cancel — executing workflows and their history."""

from __future__ import annotations

from typing import Any

import click
from rich.markup import escape
from rich.table import Table

from highhx.commands import App, pass_app
from highhx.commands.code.run import parse_pairs
from highhx.core.errors import NotFoundError, WorkflowError
from highhx.core.result import WorkflowResult
from highhx.workflows import running


def _exit(result: WorkflowResult) -> int:
    return 0 if result.ok else (130 if result.status == "cancelled" else 124 if result.status == "timeout" else 1)


def _rollback_note(app: App, result: WorkflowResult) -> None:
    if not result.rollback or not app.output.human:
        return
    app.output.heading("Rollback")
    app.output.outcomes((bool(r.get("ok")), f"{r['step']}: {r.get('detail', '')}") for r in result.rollback)


@click.command("run", short_help="Run a workflow (same as `highhx run`).")
@click.argument("name")
@click.option("--input", "-i", "inputs", multiple=True, metavar="NAME=VALUE", help="Workflow input (repeatable).")
@click.option(
    "--env", "-e", "envs", multiple=True, metavar="NAME=VALUE", help="Extra environment variable (repeatable)."
)
@pass_app
def run_workflow(app: App, name: str, inputs: tuple[str, ...], envs: tuple[str, ...]) -> int:
    """Run workflow NAME. Every step is classified and approved as it runs; with
    `on_failure: rollback` completed steps are undone when a later step fails.
    A failed or interrupted run can be resumed with `highhx workflow resume ID`."""
    result = app.workflows.run(name, inputs=parse_pairs(inputs, "--input"), env=parse_pairs(envs, "--env"))
    app.output.emit(result.to_dict())
    _rollback_note(app, result)
    if not result.ok and app.output.human and result.execution_id:
        app.output.note(f"Resume from the first unfinished step: highhx workflow resume {result.execution_id}")
    return _exit(result)


@click.command("resume", short_help="Resume a failed or cancelled workflow run.")
@click.argument("execution_id")
@click.option(
    "--env", "-e", "envs", multiple=True, metavar="NAME=VALUE", help="Environment for the resumed run (not stored)."
)
@pass_app
def resume_workflow(app: App, execution_id: str, envs: tuple[str, ...]) -> int:
    """Run the workflow of EXECUTION_ID again with the same inputs, reusing every step that
    already succeeded (and its outputs); the rest runs as usual, with approvals. Extra
    environment variables given to the first run are not stored — pass them again with -e."""
    record_id = _full_id(app, execution_id)
    key, inputs, completed = app.workflows.resume_state(record_id)
    if app.output.human:
        app.output.info(
            f"Resuming {key} ({record_id}): {len(completed)} completed step(s) are reused"
            + (f" — {', '.join(sorted(completed))}" if completed else "")
        )
    result = app.workflows.run(key, inputs=inputs, env=parse_pairs(envs, "--env"), resume=completed)
    app.output.emit({**result.to_dict(), "resumed_from": record_id, "reused_steps": sorted(completed)})
    _rollback_note(app, result)
    return _exit(result)


@click.command("cancel", short_help="Cancel a running workflow (in any HighhX process).")
@click.argument("execution_id")
@pass_app
def cancel_workflow(app: App, execution_id: str) -> int:
    """Interrupt the process running EXECUTION_ID. The run stops its commands, is recorded as
    cancelled and can be resumed later."""
    entry = running.find(execution_id)
    if entry is None:
        raise NotFoundError(
            f"No running workflow matches {execution_id}.", hint="See `highhx workflow runs --running`."
        )
    ok = running.cancel(entry)
    out = app.output
    out.emit(
        {"execution_id": entry.execution_id, "workflow": entry.workflow, "pid": entry.pid, "cancelled": ok},
        lambda: (out.success if ok else out.error)(
            f"{'Cancelling' if ok else 'Could not cancel'} {entry.workflow} ({entry.execution_id}, pid {entry.pid})"
            + ("" if ok else " — it runs in this process; press Ctrl+C there")
        ),
    )
    return 0 if ok else 1


@click.command("runs", short_help="Recent workflow runs and their status.")
@click.option("--running", "only_running", is_flag=True, help="Only runs in progress right now.")
@click.option("--limit", type=click.IntRange(1, 500), default=20, show_default=True)
@pass_app
def list_runs(app: App, only_running: bool, limit: int) -> int:
    """Workflow executions, newest first: status, duration, failed steps. Resume a failed run
    with `highhx workflow resume ID`, cancel a running one with `highhx workflow cancel ID`."""
    active = {r.execution_id: r for r in running.running()}
    rows: list[dict[str, Any]] = []
    if not only_running and app.history is not None:
        for record in app.history.list(kind="workflow", limit=limit):
            rows.append(
                {
                    "execution_id": record.id,
                    "workflow": record.name,
                    "status": "running" if record.id in active else record.status,
                    "started_at": record.started_at,
                    "duration": record.duration,
                    "error": record.error,
                }
            )
    known = {r["execution_id"] for r in rows}
    for entry in active.values():
        if entry.execution_id not in known:
            rows.insert(0, {"execution_id": entry.execution_id, "workflow": entry.workflow, "status": "running"})
    out = app.output
    out.emit(
        {"runs": rows},
        lambda: (
            out.table(
                ["id", "workflow", "status", "started", "duration", "error"],
                [
                    (
                        r["execution_id"],
                        r["workflow"],
                        r["status"],
                        str(r.get("started_at") or "")[:19].replace("T", " "),
                        f"{r['duration']:.1f}s" if r.get("duration") is not None else "-",
                        (r.get("error") or "")[:60],
                    )
                    for r in rows
                ],
            )
            if rows
            else out.info("No workflow runs yet.")
        ),
    )
    return 0


@click.command("inspect", short_help="A workflow's steps, or a run's step-by-step result.")
@click.argument("target")
@pass_app
def inspect_workflow(app: App, target: str) -> int:
    """TARGET is a workflow name (shows its steps: kind, dependencies, conditions, approvals,
    retries and rollback) or a run id (shows each step's status, exit code and error)."""
    out = app.output
    record = None
    if app.history is not None:
        try:
            record = app.history.get(_full_id(app, target))
        except (NotFoundError, WorkflowError):
            record = None
    if record is not None and record.kind == "workflow":
        run = record

        def render_run() -> None:
            out.kv(
                {
                    "run": run.id,
                    "workflow": run.name,
                    "status": run.status,
                    "started": run.started_at,
                    "duration": f"{run.duration or 0:.1f}s",
                    "error": run.error or "-",
                }
            )
            out.table(
                ["step", "status", "exit", "duration", "error"],
                [
                    (
                        s.step_id,
                        s.status,
                        "-" if s.exit_code is None else s.exit_code,
                        f"{s.duration or 0:.1f}s",
                        (s.error or "")[:70],
                    )
                    for s in run.steps
                ],
            )

        out.emit(run.to_dict(), render_run)
        return 0
    spec = app.workflows.load(target)
    stages = app.workflows.plan(spec)
    stage_of = {step: index for index, stage in enumerate(stages, start=1) for step in stage}
    rows: list[dict[str, Any]] = []
    for step in spec.steps:
        kind = f"action {step.action}" if step.action else (f"uses {step.uses}" if step.uses else "run")
        undo = step.rollback
        rollback = (
            "-"
            if undo is None
            else "compensate"
            if undo.compensate
            else f"action {undo.action}"
            if undo.action
            else "run"
        )
        rows.append(
            {
                "step": step.id,
                "stage": stage_of.get(step.id),
                "kind": kind,
                "depends_on": step.depends_on,
                "if": step.condition,
                "approval": step.approval is not None,
                "retry": step.retry.attempts,
                "rollback": rollback,
                "commands": step.run,
            }
        )

    def render() -> None:
        out.kv(
            {
                "workflow": spec.name,
                "description": spec.description or "-",
                "on failure": spec.on_failure,
                "triggers": ", ".join(spec.triggers) or "-",
                "source": str(spec.source or "-"),
            }
        )
        table = Table(box=None, header_style="dim", pad_edge=False)
        for column in ("stage", "step", "kind", "needs", "if", "approval", "retry", "rollback"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                str(row["stage"]),
                escape(str(row["step"])),
                escape(str(row["kind"])),
                escape(", ".join(row["depends_on"]) or "-"),
                escape(str(row["if"] or "-")),
                "yes" if row["approval"] else "-",
                str(row["retry"]),
                escape(str(row["rollback"])),
            )
        out.print(table)

    out.emit({"workflow": spec.name, "on_failure": spec.on_failure, "steps": rows}, render)
    return 0


def _full_id(app: App, prefix: str) -> str:
    """A run id from a unique prefix of a recent workflow run (the full id passes through)."""
    if app.history is None:
        return prefix
    matches = [r.id for r in app.history.list(kind="workflow", limit=500) if r.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return prefix
