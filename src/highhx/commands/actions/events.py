"""highhx events — the structured event log (sessions, intents, actions, approvals, workflows, agent)."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("events", short_help="Structured execution events (actions, approvals, workflows, agent).")
@click.option("--session", "session", metavar="ID", help="Only this interactive session.")
@click.option("--type", "prefix", metavar="PREFIX", help="Only events whose name starts with this (e.g. action.).")
@click.option("--limit", "-n", type=click.IntRange(1, 5000), default=50, show_default=True)
@pass_app
def events(app: App, session: str | None, prefix: str | None, limit: int) -> int:
    """The most recent events, oldest first. Events are stored as JSON Lines (secrets
    redacted) under the HighhX data directory, one file per day."""
    from highhx.actions.events import events_dir, read_events

    records = read_events(session=session, prefix=prefix, limit=limit)
    out = app.output

    def render() -> None:
        if not records:
            out.info(f"No events yet ({events_dir()}).")
            return
        out.table(
            ["time", "event", "session", "detail"],
            [
                (
                    str(r.get("ts", ""))[:19].replace("T", " "),
                    r.get("event", ""),
                    str(r.get("session", "-"))[-10:],
                    " ".join(
                        str(r[k])
                        for k in ("action", "workflow", "rule", "risk", "status", "summary", "error", "text")
                        if r.get(k) not in (None, "")
                    )[:90],
                )
                for r in records
            ],
        )

    out.emit({"events": records}, render)
    return 0
