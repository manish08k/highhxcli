"""highhx trajectories — what agent tasks saw, did and concluded."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


def _store(app: App):  # type: ignore[no-untyped-def]
    from highhx.trajectories import TrajectoryStore

    return TrajectoryStore.for_app(app)


@click.group("trajectories", cls=DefaultGroup, default_command="list", short_help="Recorded agent task trajectories.")
def trajectories() -> None:
    """Each agent task's steps — observation, action, result, verification, reflection and how
    each target was grounded — stored redacted. Similar past tasks inform planning, and their
    selectors inform grounding."""


@trajectories.command("list", short_help="Recent tasks.")
@click.option("--limit", "-n", type=click.IntRange(1, 500), default=20, show_default=True)
@click.option("--status", help="Only tasks with this status.")
@pass_app
def trajectories_list(app: App, limit: int, status: str | None) -> int:
    """Recent tasks: id, status, steps, goal."""
    from highhx.trajectories import summarize

    items = [summarize(t) for t in _store(app).recent(limit=limit, status=status)]
    app.output.emit(items, lambda: app.output.table(["task", "status", "steps", "seconds", "goal"], [(i["id"], i["status"], i["steps"], i["seconds"], i["task"]) for i in items]))
    return 0


@trajectories.command("show", short_help="One task, step by step.")
@click.argument("task_id")
@pass_app
def trajectories_show(app: App, task_id: str) -> int:
    """Every step with its action, outcome, grounding strategy and reflection."""
    trajectory = _store(app).load(task_id)
    app.output.emit(trajectory.to_dict(), lambda: app.output.plain(trajectory.describe()))
    return 0


@trajectories.command("search", short_help="Find similar past tasks.")
@click.argument("query")
@pass_app
def trajectories_search(app: App, query: str) -> int:
    """Tasks that share words with QUERY (lexical; a model embedding can replace it)."""
    hits = _store(app).search(query, limit=10)
    data = [{"id": h.trajectory.id, "score": round(h.score, 3), "status": h.trajectory.status, "task": h.trajectory.task} for h in hits]
    app.output.emit(data, lambda: app.output.table(["task", "score", "status", "goal"], [(d["id"], d["score"], d["status"], d["task"]) for d in data]))
    return 0
