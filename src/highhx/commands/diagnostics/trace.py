"""highhx trace"""

from __future__ import annotations

import click
from rich.tree import Tree

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.observability.tracing import Span, load_trace
from highhx.utils.time import format_duration


@click.command("trace", short_help="Timing tree (spans) of an execution.")
@click.argument("execution_id", required=False)
@pass_app
def trace(app: App, execution_id: str | None) -> int:
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
