"""highhx plugin trust"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("trust", short_help="Allow an installed plugin's current code to run.")
@click.argument("name")
@click.option("--global", "global_", is_flag=True, help="The plugin is installed for your user.")
@pass_app
def trust(app: App, name: str, global_: bool) -> int:
    """Record that you reviewed and trust the current files of plugin NAME.

    Needed for code plugins that arrive with a repository (for example a teammate
    committed .highhx/plugins/NAME) or after a plugin's files changed. Trust is
    stored per user and per exact file contents (SHA-256); any later change
    blocks the code again until you trust it anew. Code still only runs when
    plugins.allow_code is true.
    """
    manifest = app.plugin_manager.trust_installed(name, global_=global_)
    app.output.emit(
        {"trusted": manifest.name, "version": manifest.version, "dry_run": app.options.dry_run},
        lambda: app.output.success(
            f"{'Would trust' if app.options.dry_run else 'Trusted'} {manifest.name} {manifest.version}"
        ),
    )
    return 0
