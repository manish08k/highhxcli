"""highhx security config"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.security.main import FAIL_ON, exit_code, render_report, run_scan


@click.command("config", short_help="Unsafe configuration, permissions and risky workflow steps.")
@FAIL_ON
@pass_app
def config_check(app: App, fail_on: str) -> int:
    """Check HighhX and project configuration for unsafe settings, loose file permissions
    (.env, private keys, hooks) and dangerous workflow steps without `approval`."""
    report = run_scan(app, ["config", "permissions", "workflows"])
    app.output.emit(report.to_dict(), lambda: render_report(app, report))
    return exit_code(report, fail_on)
