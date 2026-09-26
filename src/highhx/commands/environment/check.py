"""highhx env check"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.result import CheckStatus, summarize_checks


@click.command("check", short_help="Validate required variables, patterns and choices.")
@click.option("--profile", "-p", "profile_name", metavar="NAME", help="Environment profile (default: the active one).")
@pass_app
def check(app: App, profile_name: str | None) -> int:
    """Check that every required variable is set for the profile (values are not shown)."""
    app.require_project()
    manager = app.environment
    name = profile_name or manager.active_profile()
    manager.require_profile(name)
    checks = manager.check(name)
    failed = any(c.status == CheckStatus.FAIL for c in checks)
    out = app.output

    def render() -> None:
        out.heading(f"Environment check: {name}")
        for c in checks:
            out.check(c)

    out.emit(
        {
            "profile": name,
            "ok": not failed,
            "summary": summarize_checks(checks),
            "checks": [c.to_dict() for c in checks],
        },
        render,
    )
    return 1 if failed else 0
