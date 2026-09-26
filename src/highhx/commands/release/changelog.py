"""highhx changelog"""

from __future__ import annotations

import click

from highhx.approvals.risk import RiskLevel
from highhx.commands import App, pass_app
from highhx.release.changelog import insert_section


@click.command("changelog", short_help="Generate release notes from git history.")
@click.option("--write", is_flag=True, help="Insert the section into the changelog file.")
@click.option("--version", "version_name", metavar="X.Y.Z", help="Version heading (default: suggested next version).")
@pass_app
def changelog(app: App, write: bool, version_name: str | None) -> int:
    """Group commits since the last version tag by Conventional Commit type.
    Only real commits are used — nothing is invented. Prints unless --write."""
    plan = app.releases.plan("auto", explicit=version_name)
    out = app.output

    def render() -> None:
        out.plain(plan.changelog)
        if write and not app.options.dry_run:
            out.success(f"Updated {app.config.release.changelog}")

    if write:
        path = app.root / app.config.release.changelog
        app.engine.approve(f"Update {path.name}", RiskLevel.NORMAL, policy_action="changelog")
        if not app.options.dry_run:
            insert_section(path, plan.changelog)
    out.emit(
        {
            "version": plan.next,
            "since": plan.since,
            "commits": len(plan.commits),
            "markdown": plan.changelog,
            "written": write and not app.options.dry_run,
        },
        render,
    )
    return 0
