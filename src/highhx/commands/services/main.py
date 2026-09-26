"""highhx services"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("services", short_help="Status, health, ports and PIDs of services.")
@pass_app
def services(app: App) -> int:
    """Show HighhX-managed services (from config) and Docker Compose services."""
    rows = [s.to_dict() for s in app.services.status()] if app.initialized else []
    compose = []
    if app.docker.installed() and app.docker.has_compose():
        compose = [s.to_dict() | {"source": "compose"} for s in app.docker.compose_ps()]
    out = app.output

    def render() -> None:
        table = []
        for r in rows:
            health = "-" if r["healthy"] is None else ("healthy" if r["healthy"] else "unhealthy")
            table.append(
                (
                    r["name"],
                    "highhx",
                    "running" if r["running"] else "stopped",
                    r["pid"] or "-",
                    r["port"] or "-",
                    health if r["running"] else "-",
                )
            )
        for c in compose:
            table.append((c["name"], "compose", c["state"], "-", c["ports"] or "-", c["health"] or "-"))
        if table:
            out.table(["service", "source", "state", "pid", "port", "health"], table)
        else:
            out.info("No services configured (see `services:` in .highhx/config.yaml) and no Compose services found.")

    out.emit({"services": rows, "compose": compose}, render)
    return 0
