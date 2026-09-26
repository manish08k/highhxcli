"""highhx docker up"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("up", short_help="Start Compose services (detached).")
@click.argument("services", nargs=-1)
@click.option("--build", is_flag=True, help="Build images first.")
@click.option("--attach", is_flag=True, help="Run in the foreground instead of detached.")
@pass_app
def up(app: App, services: tuple[str, ...], build: bool, attach: bool) -> int:
    """Start the project's Compose services (all, or SERVICES) in the background;
    --build rebuilds images first, --attach runs in the foreground."""
    result = app.docker.compose_up(list(services) or None, build=build, detach=not attach)
    app.output.emit(
        result.to_dict(include_output=False),
        lambda: app.output.success("Compose services started") if result.ok else None,
    )
    return 0
