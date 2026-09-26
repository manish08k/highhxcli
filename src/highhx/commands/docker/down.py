"""highhx docker down"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("down", short_help="Stop Compose services.")
@click.option("--volumes", is_flag=True, help="Also DELETE named volumes (critical; asks to confirm).")
@click.option("--remove-orphans", is_flag=True)
@pass_app
def down(app: App, volumes: bool, remove_orphans: bool) -> int:
    """Stop and remove the project's Compose containers. --volumes also deletes named
    volumes and their data, which is critical risk and needs typed confirmation."""
    result = app.docker.compose_down(volumes=volumes, remove_orphans=remove_orphans)
    app.output.emit(
        result.to_dict(include_output=False),
        lambda: app.output.success("Compose services stopped") if result.ok else None,
    )
    return 0
