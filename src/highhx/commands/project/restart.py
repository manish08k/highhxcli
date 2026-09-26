"""highhx restart"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("restart", short_help="Restart background services.")
@click.argument("services", nargs=-1)
@pass_app
def restart(app: App, services: tuple[str, ...]) -> int:
    """Stop, then start the given services (or all)."""
    app.require_project()
    results = app.services.restart(list(services) or None)
    out = app.output
    out.emit({"services": results}, lambda: out.outcomes((True, f"{r['name']}: {r['status']}") for r in results))
    return 0
