"""highhx security deps"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.security.main import FAIL_ON, exit_code, render_report, run_scan


@click.command("deps", short_help="Known vulnerabilities in dependencies (local audit tools).")
@FAIL_ON
@pass_app
def deps_check(app: App, fail_on: str) -> int:
    """Report known vulnerabilities in dependencies using the ecosystem's local audit tool
    (pip-audit, npm/pnpm/yarn audit, cargo audit). Ecosystems without an installed tool are listed as skipped."""
    report = run_scan(app, ["dependencies"])
    app.output.emit(report.to_dict(), lambda: render_report(app, report))
    return exit_code(report, fail_on)
