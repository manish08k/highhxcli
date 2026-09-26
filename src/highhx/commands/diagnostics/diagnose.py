"""highhx diagnose"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.diagnostics.diagnose import diagnose as run_diagnose


@click.command("diagnose", short_help="Identify concrete problems (and which `repair` can fix).")
@pass_app
def diagnose(app: App) -> int:
    """Find concrete failures: invalid config or workflows, tools missing for
    configured commands, missing required variables, port conflicts, stale
    state, insecure .env permissions and recent failed runs."""
    found = run_diagnose(app)
    errors = [d for d in found if d.severity == "error"]
    out = app.output

    def render() -> None:
        if not found:
            out.success("No problems found.")
            return
        symbols = {"error": "fail", "warning": "warn", "info": "skip"}
        for d in found:
            out.markup(
                f"{out.status_symbol(symbols[d.severity])} {d.problem}"
                + (" [muted](repairable)[/muted]" if d.repair else "")
            )
            if d.detail:
                out.note(f"    {d.detail}")
            if d.fix:
                out.note(f"    fix: {d.fix}")
        if any(d.repair for d in found):
            out.plain("")
            out.info("Run `highhx repair` to apply the safe automatic repairs.")

    out.emit({"ok": not errors, "problems": [d.to_dict() for d in found]}, render)
    return 1 if errors else 0
