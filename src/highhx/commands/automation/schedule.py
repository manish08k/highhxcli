"""highhx schedule"""

from __future__ import annotations

import click

from highhx.automation.scheduler import ScheduleRunner
from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.config.schema import ScheduleConfig
from highhx.core.errors import HighhXError, NotFoundError
from highhx.execution.command import CommandSpec


def _runner(app: App) -> ScheduleRunner:
    def run(schedule: ScheduleConfig) -> bool:
        app.output.info(f"[schedule {schedule.name}] starting")
        try:
            if schedule.workflow:
                ok = app.workflows.run(schedule.workflow).ok
            else:
                assert schedule.run
                ok = app.engine.run(CommandSpec(schedule.run, cwd=app.root, name=f"schedule:{schedule.name}")).ok
        except HighhXError as exc:
            app.output.error(f"[schedule {schedule.name}] {exc.message}")
            return False
        (app.output.success if ok else app.output.error)(f"[schedule {schedule.name}] {'finished' if ok else 'failed'}")
        return ok

    return ScheduleRunner(app.load_config().schedules, run, app.state)


@click.group(
    "schedule",
    cls=DefaultGroup,
    default_command="list",
    short_help="Cron-style schedules run by a local foreground scheduler.",
)
def schedule() -> None:
    """Schedules are defined under `schedules:` in .highhx/config.yaml (5-field
    cron syntax). `highhx schedule run` keeps running and executes them on time;
    no system cron or background service is installed."""


@schedule.command("list", short_help="Show schedules with last and next run.")
@pass_app
def list_schedules(app: App) -> int:
    """Show each schedule from `schedules:` with its cron expression, what it runs, when it
    last ran and when it will run next (UTC)."""
    app.require_project()
    rows = [i.to_dict() for i in _runner(app).info()]
    out = app.output
    out.emit(
        {"schedules": rows},
        lambda: (
            out.table(
                ["name", "cron", "runs", "last run", "next run (UTC)"],
                [(r["name"], r["cron"], r["target"], r["last_run"] or "never", r["next_run"]) for r in rows],
            )
            if rows
            else out.info("No schedules configured.")
        ),
    )
    return 0


@schedule.command("run", short_help="Run the scheduler in the foreground (Ctrl+C to stop).")
@click.option("--once", is_flag=True, help="Run due schedules once and exit.")
@pass_app
def run_scheduler(app: App, once: bool) -> int:
    """Keep running in the foreground and execute schedules when they are due.
    --once runs whatever is due right now and exits (useful from system cron / CI)."""
    app.require_project()
    runner = _runner(app)
    if not runner.schedules:
        raise NotFoundError("No schedules configured.", hint="Add `schedules:` to .highhx/config.yaml.")
    if once:
        results = runner.tick()
        app.output.emit(
            {"ran": [{"name": n, "ok": ok} for n, ok in results]},
            lambda: app.output.info(f"{len(results)} schedule(s) were due"),
        )
        return 0 if all(ok for _, ok in results) else 1
    app.output.info(f"Scheduler running with {len(runner.schedules)} schedule(s) — Ctrl+C to stop")
    try:
        runner.run_forever(app.ctx.cancel)
    except KeyboardInterrupt:
        app.ctx.cancel.cancel("interrupted")
    return 0


@schedule.command("trigger", short_help="Run one schedule now.")
@click.argument("name")
@pass_app
def trigger_schedule(app: App, name: str) -> int:
    """Run the schedule NAME immediately, regardless of its cron expression."""
    app.require_project()
    runner = _runner(app)
    match = next((s for s in runner.schedules if s.name == name), None)
    if match is None:
        raise NotFoundError(f"No schedule named '{name}'.")
    ok = runner.run_callback(match)
    return 0 if ok else 1
