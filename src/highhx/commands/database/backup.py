"""highhx db backup"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("backup", short_help="Back up the database (or --list backups).")
@click.option("--list", "list_only", is_flag=True, help="List existing backups.")
@pass_app
def backup(app: App, list_only: bool) -> int:
    """Write a backup of the database to database.backups_dir (SQLite: online backup;
    PostgreSQL: pg_dump -Fc; MySQL: mysqldump). --list shows existing backups."""
    app.require_project()
    manager = app.database
    out = app.output
    if list_only:
        files = manager.backups()
        out.emit(
            {"backups": [f.to_dict() for f in files]},
            lambda: (
                out.table(["backup", "size"], [(f.path.name, f.to_dict()["human_size"]) for f in files])
                if files
                else out.info("No backups yet.")
            ),
        )
        return 0
    path = manager.backup()
    out.emit(
        {"backup": str(path), "dry_run": app.options.dry_run},
        lambda: out.success(
            f"{'Would write' if app.options.dry_run else 'Backup written to'} {path.relative_to(app.root) if path.is_relative_to(app.root) else path}"
        ),
    )
    return 0
