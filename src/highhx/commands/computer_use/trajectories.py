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


# ------------------------------------------------------------------- artifacts
@trajectories.group("artifacts", cls=DefaultGroup, default_command="list", short_help="Screenshots, downloads and files tasks produced.")
def artifacts() -> None:
    """Files computer-use actions produced — screenshots, downloads, generated files — stored once
    by checksum with an id, type, size, the task and action that made them, and a retention."""


def _artifacts(app: App):  # type: ignore[no-untyped-def]
    from highhx.artifacts import ArtifactStore

    return ArtifactStore.for_app(app)


@artifacts.command("list", short_help="Artifacts (newest last).")
@click.option("--task", "task_id", default="", help="Only this task's artifacts.")
@click.option("--kind", default="", help="screenshot, download, generated …")
@pass_app
def artifacts_list(app: App, task_id: str, kind: str) -> int:
    """List artifacts: id, kind, name, size, task."""
    items = [a.to_dict() for a in _artifacts(app).list(task_id=task_id, kind=kind)]
    app.output.emit(items, lambda: app.output.table(["artifact", "kind", "name", "bytes", "task"], [(i["id"], i["kind"], i["name"], i["size"], i["task_id"]) for i in items]))
    return 0


@artifacts.command("show", short_help="One artifact's metadata.")
@click.argument("artifact_id")
@pass_app
def artifacts_show(app: App, artifact_id: str) -> int:
    """An artifact's metadata (its checksum is verified)."""
    store = _artifacts(app)
    data = store.get(artifact_id).to_dict()
    store.read(artifact_id)  # verifies the checksum

    def render() -> None:
        for key, value in data.items():
            app.output.plain(f"{key}: {value}")

    app.output.emit(data, render)
    return 0


@artifacts.command("export", short_help="Copy an artifact into the project.")
@click.argument("artifact_id")
@click.argument("path")
@pass_app
def artifacts_export(app: App, artifact_id: str, path: str) -> int:
    """Write the artifact to PATH inside the project (checksum verified first)."""
    from highhx.agent.permissions import confine_path

    target = confine_path(app, path, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_artifacts(app).read(artifact_id))
    app.output.emit({"artifact": artifact_id, "path": str(target)}, lambda: app.output.success(f"wrote {target}"))
    return 0


@artifacts.command("delete", short_help="Delete an artifact.")
@click.argument("artifact_id")
@pass_app
def artifacts_delete(app: App, artifact_id: str) -> int:
    """Delete one artifact (its content goes when nothing else refers to it)."""
    _artifacts(app).delete(artifact_id)
    app.output.emit({"deleted": artifact_id}, lambda: app.output.success(f"deleted {artifact_id}"))
    return 0


@artifacts.command("prune", short_help="Delete artifacts past their retention.")
@pass_app
def artifacts_prune(app: App) -> int:
    """Remove artifacts older than their retention (30 days unless set otherwise)."""
    removed = _artifacts(app).prune()
    app.output.emit({"removed": removed}, lambda: app.output.success(f"removed {len(removed)} artifact(s)"))
    return 0
