"""highhx logs"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError


@click.command("logs", short_help="Show (or follow) the log of an execution.")
@click.argument("execution_id", required=False)
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines until the execution finishes.")
@click.option("--tail", "-n", type=int, help="Only the last N lines.")
@click.option("--service", metavar="NAME", help="Show a background service's log instead.")
@pass_app
def logs(app: App, execution_id: str | None, follow: bool, tail: int | None, service: str | None) -> int:
    """Logs of EXECUTION_ID (default: the most recent execution). Secrets are
    redacted when logs are written."""
    out = app.output
    if service:
        path = app.services.log_file(service)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        lines = lines[-tail:] if tail else lines
        out.emit({"service": service, "lines": lines}, lambda: out.lines(lines, empty="No log yet."))
        return 0
    history = app.history
    if history is None:
        raise NotFoundError("History storage is unavailable.")
    record = history.get(execution_id) if execution_id else history.latest()
    if record is None:
        out.emit({"lines": []}, lambda: out.info("No executions recorded yet."))
        return 0
    if follow and not app.options.json:
        out.note(f"following {record.kind} {record.name} ({record.id}) — Ctrl+C to stop")

        def running() -> bool:
            return history.get(record.id).status == "running"

        try:
            for line in app.logs.follow(record.id, is_running=running, stop=lambda: app.ctx.cancel.cancelled):
                out.plain(line)
        except KeyboardInterrupt:
            pass
        return 0
    lines = app.logs.read(record.id, tail=tail)

    def render() -> None:
        out.note(f"{record.kind} {record.name} · {record.status} · {record.id}")
        out.lines(lines, empty="No log lines recorded.")

    out.emit(
        {"execution": record.id, "status": record.status, "lines": lines},
        render,
    )
    return 0
