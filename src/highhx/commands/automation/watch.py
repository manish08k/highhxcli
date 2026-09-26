"""highhx watchers — list the file watchers configured for `highhx watch`."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("watchers", short_help="List configured file watchers (run them with `highhx watch`).")
@pass_app
def watchers(app: App) -> int:
    """List the file watchers configured under `watch:`. Start them with `highhx watch`."""
    items = app.load_config().watch
    out = app.output
    out.emit(
        {"watchers": [w.__dict__ for w in items]},
        lambda: (
            out.table(
                ["name", "paths", "patterns", "runs", "debounce"],
                [
                    (
                        w.name,
                        ", ".join(w.paths),
                        ", ".join(w.patterns) or "*",
                        w.run or f"workflow {w.workflow}",
                        f"{w.debounce}s",
                    )
                    for w in items
                ],
            )
            if items
            else out.info("No watchers configured (add `watch:` to .highhx/config.yaml).")
        ),
    )
    return 0
