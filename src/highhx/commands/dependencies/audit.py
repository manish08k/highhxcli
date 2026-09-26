"""highhx deps audit"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.dependencies.audit import severity_counts


@click.command("audit", short_help="Check dependencies for known vulnerabilities.")
@click.option("--manager", "-m", metavar="NAME")
@pass_app
def audit(app: App, manager: str | None) -> int:
    """Run the ecosystem's local audit tool (pip-audit, npm/pnpm/yarn audit,
    cargo audit). Exits with code 9 when vulnerabilities are reported."""
    reports = app.dependencies.audit(manager)
    out = app.output
    total = sum(len(r.vulnerabilities) for r in reports)

    def render() -> None:
        for report in reports:
            if not report.available or report.error:
                out.warn(f"{report.manager}: {report.error}")
            elif not report.vulnerabilities:
                out.success(f"{report.manager}: no known vulnerabilities reported by {report.tool}")
            else:
                counts = ", ".join(f"{n} {s}" for s, n in severity_counts(report.vulnerabilities).items())
                out.table(
                    ["package", "version", "id", "severity", "fix"],
                    [
                        (v.package, v.version or "-", v.id, v.severity, ", ".join(v.fix_versions) or "-")
                        for v in report.vulnerabilities
                    ],
                    title=f"{report.manager}: {counts}",
                )

    out.emit({"vulnerabilities": total, "reports": [r.to_dict() for r in reports]}, render)
    return 9 if total else 0
