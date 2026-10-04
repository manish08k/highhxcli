"""highhx capabilities"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.diagnostics.capabilities import AVAILABLE, capability_report, summary


@click.command("capabilities", short_help="What computer use can do on this machine (detected).")
@pass_app
def capabilities(app: App) -> int:
    """Browser, desktop, Android, OCR, sandboxes, remote computers, models, MCP, workflows:
    each one available, unavailable (and what to install), not configured, or not implemented —
    decided by looking, without starting a browser or contacting a model. "Experimental" marks
    what is implemented but not validated on a real platform in this build."""
    entries = capability_report(app)
    out = app.output

    def render() -> None:
        out.heading("HighhX capabilities")
        area = ""
        for entry in entries:
            if entry.area != area:
                area = entry.area
                out.plain("")
                out.markup(f"[title]{area}[/title]")
            mark = out.symbols.ok if entry.status == AVAILABLE else out.symbols.arrow
            tag = " (experimental)" if entry.experimental else ""
            out.plain(f"  {mark} {entry.name}{tag} — {entry.status}: {entry.detail}")
        counts = summary(entries)
        out.plain("")
        out.note(" · ".join(f"{n} {status}" for status, n in sorted(counts.items())))

    out.emit({"capabilities": [e.to_dict() for e in entries], "summary": summary(entries)}, render)
    return 0
