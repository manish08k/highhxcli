"""highhx publish"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("publish", short_help="Publish the package to its registry (critical).")
@pass_app
def publish(app: App) -> int:
    """Publish with release.publish_command or the ecosystem default
    (twine/uv/poetry, npm publish, dart pub publish, mvn deploy, cargo publish).
    Always requires typing the project name to confirm."""
    result = app.releases.publish()
    app.output.emit(
        result.to_dict(include_output=False), lambda: app.output.success("Published") if not result.dry_run else None
    )
    return 0
