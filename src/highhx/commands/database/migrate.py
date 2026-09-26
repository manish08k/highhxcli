"""highhx db migrate"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("migrate", short_help="Apply pending migrations (asks for approval).")
@pass_app
def migrate(app: App) -> int:
    """Run database.migrations.command, or apply pending NNN_name.sql files from
    migrations/ in order, each in a transaction, recording checksums."""
    app.require_project()
    applied = app.database.migrate()
    verb = "Would apply" if app.options.dry_run else "Applied"
    app.output.emit(
        {"applied": applied, "dry_run": app.options.dry_run},
        lambda: (
            app.output.success(f"{verb} {len(applied)} migration(s)")
            if applied
            else app.output.success("Database is up to date")
        ),
    )
    return 0
