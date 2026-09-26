"""highhx workflow list"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("list", short_help="List workflows.")
@pass_app
def list_workflows(app: App) -> int:
    """List the workflows in .highhx/workflows (and those contributed by plugins) with
    their description, event triggers and source."""
    refs = app.workflow_loader.list()
    out = app.output
    out.emit(
        {"workflows": [r.to_dict() for r in refs]},
        lambda: (
            out.table(
                ["workflow", "description", "triggers", "source"],
                [
                    (r.key + (" (invalid)" if r.error else ""), r.description, ", ".join(r.triggers) or "-", r.source)
                    for r in refs
                ],
            )
            if refs
            else out.info("No workflows yet. Create one with `highhx workflow create`.")
        ),
    )
    return 0
