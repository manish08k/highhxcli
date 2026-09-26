"""highhx package"""

from __future__ import annotations

import click

from highhx.commands import App, exit_code_for, pass_app
from highhx.commands.build.build import render_outcome


@click.command("package", short_help="Create distributable packages (wheel, npm tarball, jar, image …).")
@pass_app
def package(app: App) -> int:
    """Run commands.package (or the ecosystem default: `python -m build`,
    `npm pack`, `mvn package`, `docker build` …) and list the artifacts."""
    outcome = app.builder.package()
    app.output.emit(outcome.to_dict(), lambda: render_outcome(app, outcome, "package"))
    return exit_code_for(outcome.result)
