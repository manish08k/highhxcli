"""highhx repair"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.diagnostics.diagnose import diagnose
from highhx.diagnostics.repair import run_repairs


@click.command("repair", short_help="Apply safe, deterministic repairs found by `diagnose`.")
@pass_app
def repair(app: App) -> int:
    """Only well-understood fixes are automated (create missing directories,
    git-ignore state, clear stale state, restrict .env permissions, restore
    default workflows). Anything beyond SAFE risk asks for approval."""
    app.require_project()
    found = diagnose(app)
    results = run_repairs(app, found)
    out = app.output
    manual = [d for d in found if d.repair is None and d.severity == "error"]

    def render() -> None:
        if not results:
            out.success("Nothing to repair automatically.")
        for r in results:
            (out.success if r.applied else out.info)(f"{r.description}: {r.message}")
        for d in manual:
            out.warn(f"needs manual attention: {d.problem}" + (f" — {d.fix}" if d.fix else ""))

    out.emit({"repairs": [r.to_dict() for r in results], "manual": [d.to_dict() for d in manual]}, render)
    return 0
