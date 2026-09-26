"""highhx deps outdated"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("outdated", short_help="List outdated dependencies.")
@click.option("--manager", "-m", metavar="NAME")
@pass_app
def outdated(app: App, manager: str | None) -> int:
    """Ask each package manager which dependencies have newer versions."""
    reports = app.dependencies.outdated(manager)
    out = app.output

    def render() -> None:
        for report in reports:
            if report.error:
                out.warn(f"{report.manager}: {report.error}")
            elif not report.packages:
                out.success(f"{report.manager}: everything is up to date")
            else:
                out.table(
                    ["package", "current", "wanted", "latest", "update"],
                    [(p.name, p.current, p.wanted or "-", p.latest, p.update_type) for p in report.packages],
                    title=f"{report.manager} ({len(report.packages)} outdated)",
                )

    out.emit({"reports": [r.to_dict() for r in reports]}, render)
    return 0
