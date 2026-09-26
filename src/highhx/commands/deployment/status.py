"""highhx deploy status"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.utils.time import humanize_ago


@click.command("status", short_help="Latest deployments and live target status.")
@click.argument("target", required=False)
@click.option("--history", "show_history", is_flag=True, help="Show deployment history instead.")
@click.option("--no-live", is_flag=True, help="Don't query targets, only recorded state.")
@pass_app
def status(app: App, target: str | None, show_history: bool, no_live: bool) -> int:
    """Show the latest recorded deployment of each target and query the target's live
    status (status_command, Compose, kubectl …). --history lists past deployments."""
    app.require_project()
    manager = app.deployments
    out = app.output
    if show_history:
        records = manager.history(target)
        out.emit(
            {"deployments": [r.to_dict() for r in records]},
            lambda: (
                out.table(
                    ["id", "target", "version", "status", "started", "rollback of"],
                    [
                        (r.id, r.target, r.version or "-", r.status, humanize_ago(r.started_at), r.rollback_of or "")
                        for r in records
                    ],
                )
                if records
                else out.info("No deployments recorded.")
            ),
        )
        return 0
    rows = manager.status(target, live=not no_live)

    def render() -> None:
        table = []
        for row in rows:
            latest = row["latest"] or {}
            live = row.get("live") or {}
            live_text = (
                "-"
                if not live
                else ("error: " + live["error"])
                if "error" in live
                else ("up" if live.get("live") else "down" if live.get("live") is False else "unknown")
            )
            table.append(
                (
                    row["target"],
                    row["type"],
                    "yes" if row["production"] else "",
                    latest.get("version") or "-",
                    latest.get("status") or "never deployed",
                    humanize_ago(latest.get("started_at")) if latest else "-",
                    live_text,
                )
            )
        out.table(["target", "type", "prod", "version", "last status", "when", "live"], table)

    out.emit({"targets": rows}, render)
    return 0
