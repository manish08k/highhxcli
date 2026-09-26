"""highhx deps clean"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("clean", short_help="Delete installed dependency directories (node_modules, .venv …).")
@click.option("--manager", "-m", metavar="NAME")
@pass_app
def clean(app: App, manager: str | None) -> int:
    """Remove installed dependencies so they can be reinstalled from scratch.
    Shows what will be deleted and asks for confirmation."""
    removed = app.dependencies.clean(manager)
    rel = [p.relative_to(app.root).as_posix() for p in removed]
    verb = "Would remove" if app.options.dry_run else "Removed"
    app.output.emit(
        {"removed": rel, "dry_run": app.options.dry_run},
        lambda: app.output.outcomes(((True, f"{verb} {r}") for r in rel), empty="Nothing to clean."),
    )
    return 0
