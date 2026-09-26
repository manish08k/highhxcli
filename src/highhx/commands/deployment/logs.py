"""highhx deploy logs"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("logs", short_help="Logs of a target or of a recorded deployment.")
@click.argument("target", required=False)
@click.option("--id", "deployment_id", metavar="ID", help="Show the HighhX log of a recorded deployment.")
@click.option("--follow", "-f", is_flag=True)
@click.option("--tail", default=200, show_default=True, type=int)
@pass_app
def logs(app: App, target: str | None, deployment_id: str | None, follow: bool, tail: int) -> int:
    """Stream the target's own logs (logs_command, compose logs, kubectl logs),
    or with --id print what HighhX recorded while running that deployment."""
    app.require_project()
    manager = app.deployments
    if deployment_id:
        record = manager.store.get(deployment_id)
        lines = app.logs.read(record.execution_id, tail=tail) if record.execution_id else []
        app.output.emit(
            {"deployment": record.to_dict(), "log": lines},
            lambda: app.output.lines(lines, empty="No log recorded."),
        )
        return 0
    if not manager.logs(target, follow=follow, tail=tail):
        app.output.info("This target has no logs_command; showing the latest deployment log instead.")
        latest = manager.store.latest(manager.target(target).name)
        if latest and latest.execution_id:
            for line in app.logs.read(latest.execution_id, tail=tail):
                app.output.plain(line)
    return 0
