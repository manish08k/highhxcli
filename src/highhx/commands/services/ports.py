"""highhx ports"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.services.ports import is_port_free, port_owner


@click.command("ports", short_help="Which configured ports are free or in use (and by whom).")
@click.argument("ports", nargs=-1, type=click.IntRange(1, 65535))
@pass_app
def ports(app: App, ports: tuple[int, ...]) -> int:
    """Check PORTS, or every port configured for services."""
    wanted: list[tuple[int, str]] = [(p, "-") for p in ports]
    if not wanted:
        wanted = [(s.port, name) for name, s in app.config.services.items() if s.port]
    rows = []
    for port, service in wanted:
        free = is_port_free(port)
        owner = port_owner(port) if not free else None
        rows.append(
            {
                "port": port,
                "service": service,
                "free": free,
                "pid": owner.pid if owner else None,
                "process": owner.process if owner else None,
            }
        )
    out = app.output
    out.emit(
        {"ports": rows},
        lambda: (
            out.table(
                ["port", "service", "state", "pid", "process"],
                [
                    (r["port"], r["service"], "free" if r["free"] else "in use", r["pid"] or "-", r["process"] or "-")
                    for r in rows
                ],
            )
            if rows
            else out.info("No ports configured; pass port numbers, e.g. `highhx ports 3000 8000`.")
        ),
    )
    return 0
