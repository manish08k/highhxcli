"""highhx history"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.utils.time import format_duration, humanize_ago


@click.command("history", short_help="Execution history (or details of one execution).")
@click.argument("execution_id", required=False)
@click.option("--limit", "-n", default=20, show_default=True, type=click.IntRange(1, 1000))
@click.option("--kind", metavar="KIND", help="Filter: command, workflow, deploy, release …")
@click.option(
    "--status", "status_filter", type=click.Choice(["success", "failed", "running", "cancelled", "timeout", "skipped"])
)
@pass_app
def history(app: App, execution_id: str | None, limit: int, kind: str | None, status_filter: str | None) -> int:
    """Without EXECUTION_ID, list recent executions (filter with --kind/--status). With an
    id (or unique prefix), show its details and steps."""
    store = app.history
    if store is None:
        raise NotFoundError("History storage is unavailable.")
    out = app.output
    if execution_id:
        record = store.get(execution_id)

        def render_one() -> None:
            out.heading(f"{record.kind} {record.name}")
            out.kv(
                {
                    "id": record.id,
                    "status": record.status,
                    "exit code": record.exit_code,
                    "started": record.started_at,
                    "duration": format_duration(record.duration),
                    "command": record.command,
                    "cwd": record.cwd,
                    "error": record.error,
                    "trace": record.trace_id,
                }
            )
            if record.steps:
                out.table(
                    ["step", "status", "exit", "duration", "command / error"],
                    [
                        (
                            s.step_id,
                            s.status,
                            s.exit_code if s.exit_code is not None else "-",
                            format_duration(s.duration),
                            s.error or s.command or "",
                        )
                        for s in record.steps
                    ],
                )
            out.note(f"log: highhx logs {record.id}")

        out.emit(record.to_dict(), render_one)
        return 0
    records = store.list(limit=limit, kind=kind, status=status_filter)
    out.emit(
        {"executions": [r.to_dict() for r in records]},
        lambda: (
            out.table(
                ["id", "kind", "name", "status", "exit", "duration", "when"],
                [
                    (
                        r.id,
                        r.kind,
                        r.name,
                        r.status,
                        r.exit_code if r.exit_code is not None else "-",
                        format_duration(r.duration),
                        humanize_ago(r.started_at),
                    )
                    for r in records
                ],
            )
            if records
            else out.info("No executions recorded yet.")
        ),
    )
    return 0
