"""highhx security"""

from __future__ import annotations

from collections.abc import Iterable

import click

from highhx.commands import App
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import HighhXError
from highhx.security.report import SEVERITIES, SecurityReport
from highhx.security.scanner import ScanContext, SecurityScanner


def run_scan(app: App, checks: Iterable[str], *, include_untracked: bool | None = None) -> SecurityReport:
    repo = app.git_repo
    in_repo = repo.is_repo()
    try:
        deps = app.dependencies if app.dependencies.adapters else None
    except HighhXError:
        deps = None

    def env_values() -> dict[str, dict[str, str]]:
        if not app.initialized:
            return {}
        manager = app.environment
        return {name: manager.resolve(name).values for name in manager.profile_names()}

    ctx = ScanContext(
        root=app.root,
        config=app.config,
        policy=app.policy,
        tracked_files=repo.tracked_files if in_repo else None,
        is_ignored=repo.is_ignored if in_repo else None,
        workflow_loader=app.workflow_loader if app.initialized else None,
        dependency_manager=deps,
        env_values=env_values,
    )
    with app.engine.operation("security", "scan"):
        return SecurityScanner(ctx).run(checks, include_untracked=include_untracked)


def render_report(app: App, report: SecurityReport) -> None:
    out = app.output
    counts = report.counts()
    for note in report.notes:
        out.note(note)
    if report.findings:
        out.table(
            ["severity", "category", "finding", "location", "fingerprint"],
            [(f.severity, f.category, f.title, f.location, f.fingerprint) for f in report.sorted()],
        )
        for finding in report.sorted()[:10]:
            if finding.remediation:
                out.detail(f"{finding.location}: {finding.remediation}")
    summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "no findings"
    (out.warn if report.findings else out.success)(
        f"{summary} ({report.files_scanned} files, checks: {', '.join(report.checks_run)})"
    )
    out.note("HighhX reports concrete findings from local checks; no findings does not mean the project is secure.")
    if report.suppressed:
        out.note(f"{report.suppressed} finding(s) suppressed by security.allowlist")


def exit_code(report: SecurityReport, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    return 9 if report.at_least(fail_on) else 0


FAIL_ON = click.option(
    "--fail-on",
    type=click.Choice([*SEVERITIES, "never"]),
    default="high",
    show_default=True,
    help="Exit with code 9 if a finding at or above this severity exists.",
)


@click.group(
    "security",
    cls=DefaultGroup,
    default_command="scan",
    short_help="Local security checks (secrets, permissions, config, deps).",
)
def security() -> None:
    """Local-first security checks. Findings are concrete and located; secret
    values are never printed or logged."""


def _register() -> None:
    from highhx.commands.security.config import config_check
    from highhx.commands.security.dependencies import deps_check
    from highhx.commands.security.report import report
    from highhx.commands.security.scan import scan
    from highhx.commands.security.secrets import secrets

    for command in (scan, secrets, deps_check, config_check, report):
        security.add_command(command)


_register()
