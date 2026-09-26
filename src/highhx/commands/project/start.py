"""highhx start"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("start", short_help="Start configured background services.")
@click.argument("services", nargs=-1)
@pass_app
def start(app: App, services: tuple[str, ...]) -> int:
    """Start services from `services:` in .highhx/config.yaml (dependencies first),
    waiting until each one is healthy."""
    app.require_project()
    results = app.services.start(list(services) or None)
    out = app.output

    def render() -> None:
        for row in results:
            if row["status"] in ("started", "already running"):
                out.success(
                    f"{row['name']}: {row['status']} (pid {row.get('pid')})"
                    + (f" — {row['message']}" if row.get("message") else "")
                )
            else:
                out.info(f"{row['name']}: {row['status']}")

    out.emit({"services": results}, render)
    return 0
