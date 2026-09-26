"""highhx plugin search"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("search", short_help="Search the configured plugin indexes (local files/dirs).")
@click.argument("query", default="")
@pass_app
def search(app: App, query: str) -> int:
    """Search plugins listed in plugins.index (JSON index files or directories
    of plugins). Works offline; nothing is fetched from the network."""
    results = app.plugin_manager.search(query)
    out = app.output
    if not app.config.plugins.index and not app.options.json:
        out.note("No plugin indexes configured (plugins.index in .highhx/config.yaml).")
    out.emit(
        {"results": results},
        lambda: (
            out.table(
                ["plugin", "version", "description", "source", "installed"],
                [
                    (r["name"], r["version"] or "-", r["description"], r["source"], r["installed"] or "")
                    for r in results
                ],
            )
            if results
            else out.info("No matching plugins.")
        ),
    )
    return 0
