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


@trace.command("timeline", short_help="Every event of a task trace, filterable (the debugger view).")
@click.argument("trace_id")
@click.option("--component", default="", help="Only events of this component: browser, action, model, grounding, network …")
@click.option("--search", default="", help="Only events whose name or payload contains this text.")
@click.option("--failures", is_flag=True, help="Only failures, denials, crashes and errors.")
@click.option("--export", "fmt", type=click.Choice(["json", "jsonl", "csv"]), help="Print in this format instead of a table.")
@click.option("--output", "-o", type=click.Path(dir_okay=False), help="Write the export to a file.")
@pass_app
def trace_timeline(app: App, trace_id: str, component: str, search: str, failures: bool, fmt: str | None, output: str | None) -> int:
    """Time, event, component, step, action, latency, result and error for every event of a
    task, filtered and searchable — and exportable as JSON, JSON Lines or CSV."""
    import csv
    import io
    import json
    from pathlib import Path

    from highhx.observability.tasktrace import TraceStore

    rows = TraceStore.for_app(app).load(trace_id).timeline(component=component, search=search, failures=failures)
    if fmt:
        if fmt == "json":
            text = json.dumps(rows, indent=2, default=str)
        elif fmt == "jsonl":
            text = "\n".join(json.dumps(r, default=str) for r in rows)
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]) if rows else ["t", "event"])
            writer.writeheader()
            writer.writerows(rows)
            text = buffer.getvalue().rstrip("\n")
        if output:
            Path(output).write_text(text + "\n", encoding="utf-8")
            app.output.success(f"wrote {len(rows)} event(s) to {output}")
        else:
            click.echo(text)
        return 0
    app.output.emit(
        rows,
        lambda: app.output.table(
            ["t", "event", "step", "action", "latency", "result", "error"],
            [(f"{r['t']:.3f}", r["event"], r["step"], r["action"], "" if r["latency"] is None else f"{float(r['latency']):.3f}s", r["result"], r["error"][:60]) for r in rows],
        ),
    )
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
