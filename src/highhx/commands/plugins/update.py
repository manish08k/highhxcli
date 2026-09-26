"""highhx plugin update"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("update", short_help="Reinstall a plugin from its recorded source.")
@click.argument("name")
@click.option("--global", "global_", is_flag=True)
@pass_app
def update(app: App, name: str, global_: bool) -> int:
    """Reinstall plugin NAME from the source recorded when it was installed, showing its
    permissions again before replacing it."""
    manifest = app.plugin_manager.update(name, global_=global_)
    app.output.emit(manifest.to_dict(), lambda: app.output.success(f"Updated {manifest.name} to {manifest.version}"))
    return 0
