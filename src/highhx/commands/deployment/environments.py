"""highhx environments"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("environments", short_help="List deployment targets.")
@pass_app
def environments(app: App) -> int:
    """List the deployment targets defined under deploy.targets, their type, whether they
    are production, where they deploy to and the result of the last deployment."""
    app.require_project()
    rows = app.deployments.describe_targets()
    out = app.output
    out.emit(
        {"targets": rows},
        lambda: (
            out.table(
                ["target", "type", "production", "where", "health check", "last deployment"],
                [
                    (
                        r["name"] + (" (default)" if r["default"] else ""),
                        r["type"],
                        "yes" if r["production"] else "",
                        r["where"],
                        "yes" if r["health_check"] else "no",
                        (r["last_deployment"] or {}).get("status", "-"),
                    )
                    for r in rows
                ],
            )
            if rows
            else out.info("No deployment targets configured.")
        ),
    )
    return 0
