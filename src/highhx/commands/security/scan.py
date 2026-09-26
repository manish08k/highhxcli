"""highhx security scan"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.security.main import FAIL_ON, exit_code, render_report, run_scan
from highhx.security.scanner import ALL_CHECKS


@click.command("scan", short_help="Run all checks.")
@click.option("--check", "checks", multiple=True, type=click.Choice(ALL_CHECKS), help="Only these checks.")
@click.option("--all-files", is_flag=True, help="Scan untracked files too (default: committed files).")
@FAIL_ON
@pass_app
def scan(app: App, checks: tuple[str, ...], all_files: bool, fail_on: str) -> int:
    """Run the local security checks (secrets, permissions, config, policy, workflows,
    dependencies). By default only committed files are scanned; --all-files includes
    untracked ones. Exits with code 9 when a finding reaches --fail-on."""
    report = run_scan(app, checks or ALL_CHECKS, include_untracked=all_files or None)
    app.output.emit(report.to_dict(), lambda: render_report(app, report))
    return exit_code(report, fail_on)
