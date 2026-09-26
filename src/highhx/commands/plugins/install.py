"""highhx plugin install"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("install", short_help="Install a plugin from a directory, git URL or index name.")
@click.argument("source")
@click.option("--global", "global_", is_flag=True, help="Install for your user instead of this project.")
@pass_app
def install(app: App, source: str, global_: bool) -> int:
    """Validate the manifest, show its permissions and whether it contains code,
    ask for approval, copy it and record its SHA-256 in the plugins lock file."""
    manifest = app.plugin_manager.install(source, global_=global_, force=app.options.force)
    out = app.output

    def render() -> None:
        if app.options.dry_run:
            out.info(f"Dry run: would install {manifest.name} {manifest.version}")
            return
        out.success(f"Installed {manifest.name} {manifest.version}")
        if manifest.has_code and not app.config.plugins.allow_code:
            out.warn("This plugin contains Python code, which stays disabled until you set plugins.allow_code: true.")

    out.emit(manifest.to_dict() | {"dry_run": app.options.dry_run}, render)
    return 0
