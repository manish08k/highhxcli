"""highhx audit"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.safety.audit import AuditLog


@click.command("audit", short_help="Audit trail of automated actions (agent and computer).")
@click.option("--limit", type=click.IntRange(1, 1000), default=30, show_default=True, help="Number of entries.")
@click.option("--session", "session_id", metavar="ID", help="Only entries from one agent session.")
@pass_app
def audit(app: App, limit: int, session_id: str | None) -> int:
    """Every action HighhX automation classified and decided on — by the agent or by
    `highhx computer` / `highhx do` — with its risk, decision (allowed, confirmed,
    denied, blocked), outcome and verification. Values are redacted."""
    out = app.output
    if app.db is None:
        out.emit({"entries": []}, lambda: out.warn("Audit storage is unavailable."))
        return 1
    records = AuditLog(app.db, app.redactor).list(limit=limit, session_id=session_id)
    out.emit(
        {"entries": [r.to_dict() for r in records]},
        lambda: (
            out.table(
                ["time", "source", "action", "risk", "decision", "status", "verified"],
                [
                    (
                        r.created_at.replace("T", " ")[:19],
                        r.source,
                        r.action,
                        r.risk,
                        r.decision,
                        r.status,
                        "-" if r.verified is None else ("yes" if r.verified else "no"),
                    )
                    for r in records
                ],
            )
            if records
            else out.info("No automated actions recorded yet.")
        ),
    )
    return 0
