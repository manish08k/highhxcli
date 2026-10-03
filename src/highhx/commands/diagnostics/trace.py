"""highhx trace"""

from __future__ import annotations

import click
from rich.tree import Tree

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import NotFoundError
from highhx.observability.tracing import Span, load_trace
from highhx.utils.time import format_duration


@click.group("trace", cls=DefaultGroup, default_command="show", short_help="Task traces and execution timing trees.")
def trace() -> None:
    """`highhx trace [ID]` shows a trace: a task trace (`tr_…` or `task_…`: what was asked,
    planned, observed, done, retried, healed, verified, and how it ended) or an execution's
    timing tree (an execution id, default: the latest). `list` and `export` work on task traces."""


@trace.command("show", short_help="One trace (default: the latest execution).")
@click.argument("execution_id", required=False)
@pass_app
def trace_show(app: App, execution_id: str | None) -> int:
    """Show the task trace for a ``tr_``/``task_`` id, or where time went in EXECUTION_ID."""
    if execution_id and execution_id.startswith(("tr_", "task_")):
        from highhx.observability.tasktrace import TraceStore

        task_trace = TraceStore.for_app(app).load(execution_id)
        app.output.emit(task_trace.to_dict(), lambda: app.output.plain(task_trace.render()))
        return 0
    return _execution_trace(app, execution_id)


@trace.command("list", short_help="Recent task traces.")
@click.option("--limit", "-n", type=click.IntRange(1, 500), default=20, show_default=True)
@pass_app
def trace_list(app: App, limit: int) -> int:
    """Recent task traces: id, task, status, number of events."""
    from highhx.observability.tasktrace import TraceStore

    items = TraceStore.for_app(app).recent(limit)
    app.output.emit(items, lambda: app.output.table(["trace", "task", "status", "events", "when", "goal"], [(i["trace_id"], i["task_id"], i["status"], i["events"], i["when"], i["goal"]) for i in items]))
    return 0


@trace.command("export", short_help="Export a task trace (JSON or JSON Lines).")
@click.argument("trace_id")
@click.option("--format", "fmt", type=click.Choice(["json", "jsonl"]), default="json", show_default=True)
@click.option("--output", "-o", type=click.Path(dir_okay=False), help="Write to a file (default: stdout).")
@pass_app
def trace_export(app: App, trace_id: str, fmt: str, output: str | None) -> int:
    """The trace with its tree and every (redacted) event."""
    import json
    from pathlib import Path

    from highhx.observability.tasktrace import TraceStore

    task_trace = TraceStore.for_app(app).load(trace_id)
    text = json.dumps(task_trace.to_dict(), indent=2) if fmt == "json" else "\n".join(json.dumps(r.to_dict()) for r in task_trace.records)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
        app.output.success(f"wrote {output}")
    else:
        click.echo(text)
    return 0


def _execution_trace(app: App, execution_id: str | None) -> int:
    """Show where time went in EXECUTION_ID (default: latest)."""
    if app.history is None or app.db is None:
        raise NotFoundError("History storage is unavailable.")
    record = app.history.get(execution_id) if execution_id else app.history.latest()
    if record is None or not record.trace_id:
        raise NotFoundError("No trace recorded for that execution.")
    roots = [
        r
        for r in load_trace(app.db, record.trace_id)
        if r.name == f"{record.kind}:{record.name}" or r.parent_id is None
    ]
    out = app.output

    def add(node: Tree, span: Span) -> None:
        style = "fail" if span.status not in ("ok", "success", "skipped") else "ok"
        child = node.add(
            f"[{style}]{span.name}[/{style}] [muted]{format_duration(span.duration)} · {span.status}[/muted]"
        )
        for sub in span.children:
            add(child, sub)

    def render() -> None:
        tree = Tree(f"[title]{record.kind} {record.name}[/title] [muted]{record.id}[/muted]")
        for span in roots:
            add(tree, span)
        out.print(tree)

    out.emit({"execution": record.id, "trace_id": record.trace_id, "spans": [s.to_dict() for s in roots]}, render)
    return 0
