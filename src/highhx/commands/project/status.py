"""highhx status"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.project.status import collect_status
from highhx.ui.dashboard import render_status


@click.command("status", short_help="Project dashboard: git, environment, services, recent runs.")
@pass_app
def status(app: App) -> int:
    """Show an overview of the project. Use --json for machine-readable output."""
    data = collect_status(app)
    app.output.emit(data, lambda: render_status(app.output, data))
    return 0
