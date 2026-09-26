"""highhx doctor"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.result import CheckStatus, summarize_checks
from highhx.diagnostics.doctor import run_doctor, suggested_actions
from highhx.ui.progress import spinner


@click.command("doctor", short_help="Check your machine and project for problems.")
@pass_app
def doctor(app: App) -> int:
    """Check the OS, Python, Git, Docker, Node, Flutter, Java, package managers,
    runtime versions, project configuration, workflows, dependencies,
    environment variables, permissions and service ports."""
    with spinner(app.output.err_console, "Running checks…", enabled=app.output.human):
        checks = run_doctor(app)
    failed = any(c.status == CheckStatus.FAIL for c in checks)
    actions = suggested_actions(checks)
    out = app.output

    def render() -> None:
        out.heading("HighhX Doctor")
        for check in checks:
            if check.status != CheckStatus.SKIP or app.options.verbose:
                out.check(check)
        if actions:
            out.plain("")
            out.markup("[title]Suggested actions[/title]")
            for action in actions:
                out.plain(f"  {out.symbols.arrow} {action}")
        counts = summarize_checks(checks)
        out.plain("")
        out.note(
            f"{counts['ok']} ok · {counts['warn']} warnings · {counts['fail']} problems"
            + ("" if app.options.verbose else f" · {counts['skip']} not applicable (see --verbose)")
        )

    out.emit(
        {
            "ok": not failed,
            "summary": summarize_checks(checks),
            "checks": [c.to_dict() for c in checks],
            "suggested_actions": actions,
        },
        render,
    )
    return 1 if failed else 0
