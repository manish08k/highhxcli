"""highhx docker logs"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("logs", short_help="Show Compose service logs.")
@click.argument("services", nargs=-1)
@click.option("--follow", "-f", is_flag=True)
@click.option("--tail", default=200, show_default=True, type=int)
@pass_app
def logs(app: App, services: tuple[str, ...], follow: bool, tail: int) -> int:
    """Print the logs of the Compose services (all, or SERVICES); --follow streams until Ctrl+C."""
    result = app.docker.compose_logs(list(services) or None, follow=follow, tail=tail)
    if app.options.json:
        app.output.json({"logs": result.stdout.splitlines()})
    return 0 if result.ok or result.status == "cancelled" else 1
