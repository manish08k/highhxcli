"""highhx db restore"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("restore", short_help="Restore from a backup (critical; safety backup first).")
@click.argument("backup", required=False)
@click.option("--no-safety-backup", is_flag=True, help="Skip the automatic backup of current data.")
@pass_app
def restore(app: App, backup: str | None, no_safety_backup: bool) -> int:
    """Restore BACKUP (file name or path; default: newest). Current data is
    overwritten, so this always requires typed confirmation."""
    app.require_project()
    source, safety = app.database.restore(backup, safety_backup=not no_safety_backup)
    out = app.output

    def render() -> None:
        if app.options.dry_run:
            out.info(f"Dry run: would restore from {source.name}")
            return
        if safety:
            out.info(f"Safety backup: {safety.name}")
        out.success(f"Restored from {source.name}")

    out.emit(
        {
            "restored_from": str(source),
            "safety_backup": str(safety) if safety else None,
            "dry_run": app.options.dry_run,
        },
        render,
    )
    return 0
