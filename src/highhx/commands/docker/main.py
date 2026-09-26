"""highhx docker"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


@click.group("docker", cls=DefaultGroup, default_command="ps", short_help="Docker Compose services (up, down, logs).")
def docker() -> None:
    """Manage the project's Docker Compose stack through a safe wrapper."""


@docker.command("ps", short_help="Show Compose services.")
@pass_app
def ps(app: App) -> int:
    """Show whether Docker is installed and running, which Compose file is used, and the
    state, health and ports of each Compose service."""
    client = app.docker
    out = app.output
    installed = client.installed()
    running = client.daemon_running() if installed else False
    services = client.compose_ps() if running else []
    data = {
        "installed": installed,
        "daemon_running": running,
        "compose_files": [p.name for p in client.compose_files()],
        "services": [s.to_dict() for s in services],
    }

    def render() -> None:
        if not installed:
            out.warn("Docker is not installed.")
        elif not running:
            out.warn("Docker is not running.")
        if not data["compose_files"]:
            out.info("No compose file in this project.")
        elif services:
            out.table(
                ["service", "state", "status", "health", "ports"],
                [(s.name, s.state, s.status, s.health or "-", s.ports) for s in services],
            )
        elif running:
            out.info("No Compose services are running. Start them with `highhx docker up`.")

    out.emit(data, render)
    return 0


def _register() -> None:
    from highhx.commands.docker.down import down
    from highhx.commands.docker.logs import logs
    from highhx.commands.docker.up import up

    for command in (up, down, logs):
        docker.add_command(command)


_register()
