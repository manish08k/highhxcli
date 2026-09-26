"""highhx stop"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("stop", short_help="Stop background services.")
@click.argument("services", nargs=-1)
@pass_app
def stop(app: App, services: tuple[str, ...]) -> int:
    """Stop services started with `highhx start` (dependents first)."""
    app.require_project()
    results = app.services.stop(list(services) or None)
    out = app.output

    def render() -> None:
        if not results:
            out.info("No services configured.")
        for row in results:
            (out.success if row["status"] == "stopped" else out.info)(f"{row['name']}: {row['status']}")

    out.emit({"services": results}, render)
    return 1 if any(r["status"] == "failed to stop" for r in results) else 0
