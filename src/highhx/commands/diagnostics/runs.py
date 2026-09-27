"""highhx runs — traces and metrics of plain-language automation runs."""

from __future__ import annotations

from typing import Any

import click
from rich.tree import Tree

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import NotFoundError


def _store(app: App) -> Any:
    from highhx.observability.runs import RunStore

    if app.db is None:
        raise NotFoundError("History storage is unavailable.", hint="See `highhx diagnose`.")
    return RunStore(app.db)


_MARK = {"succeeded": "ok", "failed": "fail", "cancelled": "muted", "skipped": "muted"}


@click.group("runs", cls=DefaultGroup, default_command="list", short_help="Traces and metrics of automation runs.")
def runs() -> None:
    """Every plain-language request HighhX handled locally: the deterministic decision, the
    JSON plan, each step's result and verification (`highhx runs show`), and
    totals (`highhx runs stats`)."""


@runs.command("list", short_help="Recent runs.")
@click.option("--limit", "-n", type=click.IntRange(1, 500), default=20, show_default=True)
@pass_app
def runs_list(app: App, limit: int) -> int:
    """The most recent runs: request, route (local, unknown, pro), status and verification."""
    rows = _store(app).list(limit)

    def render() -> None:
        if not rows:
            app.output.info('No automation runs yet — try: highhx "show git status"')
            return
        app.output.table(
            ["run", "request", "route", "status", "verification", "time"],
            [
                [
                    r["id"],
                    r["request"][:48],
                    r["route"],
                    r["status"],
                    r["verification"] or "-",
                    f"{r['duration']:.1f}s" if r["duration"] is not None else "-",
                ]
                for r in rows
            ],
        )

    app.output.emit(rows, render)
    return 0


@runs.command("show", short_help="One run: decision, plan, steps and verification.")
@click.argument("run_id", required=False)
@pass_app
def runs_show(app: App, run_id: str | None) -> int:
    """Show RUN_ID (default: the latest run)."""
    record = _store(app).get(run_id)
    if record is None:
        raise NotFoundError(f"No run {run_id!r}." if run_id else "No automation runs yet.")

    def render() -> None:
        decision = record["decision"]
        tree = Tree(f"[title]{record['id']}[/title]  [bold]{record['request']}[/bold]")
        tree.add(
            f"decision  {decision.get('decision')} [muted]· intent[/muted] {record['intent'] or '-'} "
            f"[muted]· target[/muted] {record['target'] or '-'} [muted]· risk[/muted] {record['risk'] or '-'} "
            f"[muted]· executor[/muted] {record['executor'] or '-'}"
        )
        if not record["steps"] and record["failure"]:
            tree.add(f"[muted]{record['failure']}[/muted]")
        for step in record["steps"]:
            style = _MARK.get(step["status"], "fail")
            check = step.get("verification") or {}
            node = tree.add(
                f"[{style}]{step['id']} {step['action']} {step['target']}[/{style}] "
                f"[muted]{step['catalog_action']} · {step['status']} · {step['seconds']:.1f}s[/muted]"
            )
            if check:
                node.add(
                    f"[muted]verification: {check.get('status')} {('— ' + check['detail']) if check.get('detail') else ''}[/muted]"
                )
            if step.get("error"):
                node.add(f"[fail]{step['error']}[/fail]")
        duration = f"{record['duration']:.1f}s" if record["duration"] is not None else "-"
        tree.add(f"result  {record['status']} · {record['verification'] or 'not executed'} · {duration}")
        app.output.print(tree)

    app.output.emit(record, render)
    return 0


@runs.command("stats", short_help="Totals: success rate, failures, escalations, most-used actions.")
@click.option("--limit", type=click.IntRange(1, 100_000), default=1000, show_default=True, help="Runs to include.")
@pass_app
def runs_stats(app: App, limit: int) -> int:
    """Totals over recent runs: successes and failures, average time, action and verification
    failures, Pro escalations, and the most-used actions and targets."""
    metrics = _store(app).metrics(limit).to_dict()

    def render() -> None:
        app.output.kv(
            {
                "total runs": metrics["total_runs"],
                "successful": metrics["successful_runs"],
                "failed": metrics["failed_runs"],
                "unknown requests": metrics["unknown_requests"],
                "Pro escalations": metrics["pro_escalations"],
                "average time": f"{metrics['average_seconds']}s" if metrics["average_seconds"] is not None else "-",
                "verification failures": metrics["verification_failures"],
                "unverified steps": metrics["unverified_steps"],
                "action failures": ", ".join(f"{a} ({n})" for a, n in metrics["action_failures"].items()) or "-",
                "most-used actions": ", ".join(f"{a['action']} ({a['runs']})" for a in metrics["most_used_actions"])
                or "-",
                "most-used targets": ", ".join(f"{t['target']} ({t['runs']})" for t in metrics["most_used_targets"])
                or "-",
            },
            title="Automation runs",
        )

    app.output.emit(metrics, render)
    return 0
