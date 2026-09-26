"""highhx deps"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


@click.group(
    "deps",
    cls=DefaultGroup,
    default_command="summary",
    short_help="Manage dependencies (install, update, outdated, audit, clean).",
)
def deps() -> None:
    """Dependency management for every detected ecosystem (pip/uv/poetry,
    npm/pnpm/yarn/bun, flutter/dart pub, Maven, Gradle, Cargo, Go)."""


@deps.command("summary", short_help="Show detected package managers.")
@pass_app
def summary(app: App) -> int:
    """List the package managers detected in this project, whether each tool is installed,
    its lockfile, and the command `deps install` would run."""
    rows = app.dependencies.summary()
    out = app.output
    out.emit(
        {"managers": rows},
        lambda: (
            out.table(
                ["manager", "ecosystem", "installed", "lockfile", "install command"],
                [
                    (
                        r["manager"],
                        r["ecosystem"],
                        "yes" if r["available"] else "no",
                        (r["lockfile"] or "-")
                        + ("" if not r["lockfile"] else " ✓" if r["lockfile_present"] else " (missing)"),
                        r["install"],
                    )
                    for r in rows
                ],
            )
            if rows
            else out.info("No package manager detected.")
        ),
    )
    return 0


def _register() -> None:
    from highhx.commands.dependencies.audit import audit
    from highhx.commands.dependencies.clean import clean
    from highhx.commands.dependencies.install import install
    from highhx.commands.dependencies.outdated import outdated
    from highhx.commands.dependencies.update import update

    for command in (install, update, outdated, audit, clean):
        deps.add_command(command)


_register()
