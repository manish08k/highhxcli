"""highhx plugin remove"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("remove", short_help="Uninstall a plugin.")
@click.argument("name")
@click.option("--global", "global_", is_flag=True)
@pass_app
def remove(app: App, name: str, global_: bool) -> int:
    """Uninstall plugin NAME from the project (or --global for your user)."""
    path = app.plugin_manager.remove(name, global_=global_)
    app.output.emit(
        {"removed": name, "path": str(path), "dry_run": app.options.dry_run},
        lambda: app.output.success(f"{'Would remove' if app.options.dry_run else 'Removed'} {name}"),
    )
    return 0
