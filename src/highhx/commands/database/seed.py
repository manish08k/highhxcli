"""highhx db seed"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("seed", short_help="Load seed data (asks for approval).")
@pass_app
def seed(app: App) -> int:
    """Load seed data with database.seed.command or the SQL files in seeds/.
    Dangerous (critical on protected profiles), so it asks for confirmation."""
    app.require_project()
    loaded = app.database.seed()
    app.output.emit(
        {"seeded": loaded, "dry_run": app.options.dry_run},
        lambda: app.output.success(f"{'Would load' if app.options.dry_run else 'Loaded'} {', '.join(loaded)}"),
    )
    return 0
