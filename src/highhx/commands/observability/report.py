"""highhx report"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.observability.metrics import compute_metrics
from highhx.utils.time import format_duration


@click.command("report", short_help="Success rates and durations from execution history.")
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 3650))
@pass_app
def report(app: App, days: int) -> int:
    """Summarise execution history for the last --days: runs, success rate, and median and
    p95 durations per command and workflow."""
    if app.db is None:
        raise NotFoundError("History storage is unavailable.")
    metrics = compute_metrics(app.db, days=days)
    data = metrics.to_dict()
    out = app.output

    def render() -> None:
        rate = f"{data['success_rate'] * 100:.0f}%" if data["success_rate"] is not None else "-"
        out.heading(
            f"Last {days} days: {metrics.total} runs, {metrics.succeeded} succeeded, {metrics.failed} failed ({rate})"
        )
        rows = [
            (
                m["kind"],
                m["name"],
                m["runs"],
                f"{m['success_rate'] * 100:.0f}%",
                format_duration(m["median_duration"]),
                format_duration(m["p95_duration"]),
            )
            for m in data["by_name"]
        ]
        if rows:
            out.table(["kind", "name", "runs", "success", "median", "p95"], rows)

    out.emit(data, render)
    return 0
