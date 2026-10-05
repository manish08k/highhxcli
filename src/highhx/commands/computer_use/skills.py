"""highhx skills — reusable, checked knowledge about applications and sites."""

from __future__ import annotations

from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


def _available(app: App) -> set[str]:
    from highhx.diagnostics.capabilities import AVAILABLE, capability_report

    entries = capability_report(app)
    have = set()
    for entry in entries:
        if entry.status == AVAILABLE:
            have.add(entry.area)
    return have


@click.group(
    "skills",
    cls=DefaultGroup,
    default_command="list",
    short_help="Application skills: Chrome, GitHub, VS Code, Gmail, files …",
)
def skills() -> None:
    """Skills describe how to use an application or site with HighhX's actions: where they
    apply, what they need, their actions and permissions, runnable examples, checks and known
    failure modes. Built-in skills ship with HighhX; a project adds its own in .highhx/skills/.
    Skills grant nothing: their examples run through the executor like any plan."""


@skills.command("list", short_help="Skills, and whether each is usable here.")
@pass_app
def skills_list(app: App) -> int:
    """List the skills with their problems (if any) on this machine."""
    from highhx.actions.catalog import catalog_for
    from highhx.skills import check, load

    catalog, have = catalog_for(app), _available(app)
    rows: list[dict[str, Any]] = [
        {
            "name": s.name,
            "version": s.version,
            "description": s.description,
            "problems": check(s, catalog, have),
            "source": s.source,
        }
        for s in load(app.root)
    ]
    app.output.emit(
        rows,
        lambda: app.output.table(
            ["skill", "v", "status", "description"],
            [
                (
                    r["name"],
                    r["version"],
                    "ok" if not r["problems"] else "; ".join(r["problems"])[:50],
                    r["description"][:70],
                )
                for r in rows
            ],
        ),
    )
    return 0


@skills.command("show", short_help="One skill in full.")
@click.argument("name")
@pass_app
def skills_show(app: App, name: str) -> int:
    """Everything a skill says: actions, permissions, examples, checks, failure modes."""
    from highhx.core.errors import UsageError
    from highhx.skills import load

    skill = next((s for s in load(app.root) if s.name == name), None)
    if skill is None:
        raise UsageError(f"No skill named {name!r}.", hint="See `highhx skills`.")
    data = skill.to_dict()

    def render() -> None:
        out = app.output
        out.heading(f"{skill.name} (v{skill.version})")
        out.plain(skill.description)
        out.plain(f"actions: {', '.join(skill.actions)}")
        for line in skill.permissions:
            out.plain(f"  · {line}")
        for index, example in enumerate(skill.examples, 1):
            out.plain(f"example {index}: {example.get('task')}")
        for fm in skill.failure_modes:
            out.plain(f"  if {fm['symptom']} → {fm['recovery']}")

    app.output.emit(data, render)
    return 0


@skills.command("check", short_help="Validate skills against the catalog and this machine.")
@pass_app
def skills_check(app: App) -> int:
    """Exit 1 when a skill references unknown actions or is malformed (missing capabilities are reported, not failed)."""
    from highhx.actions.catalog import catalog_for
    from highhx.skills import check, load

    catalog = catalog_for(app)
    report = {s.name: check(s, catalog) for s in load(app.root)}
    broken = {k: v for k, v in report.items() if v}

    def render() -> None:
        if not broken:
            app.output.success(f"{len(report)} skill(s) valid")
        for skill_name, problems in broken.items():
            app.output.error(f"{skill_name}: {'; '.join(problems)}")

    app.output.emit({"ok": not broken, "skills": report}, render)
    return 1 if broken else 0


@skills.command("run", short_help="Run one of a skill's examples (through the executor).")
@click.argument("name")
@click.option("--example", "index", type=int, default=1, show_default=True)
@pass_app
def skills_run(app: App, name: str, index: int) -> int:
    """Run example INDEX of skill NAME as a plan: every step classified, approved, verified, recorded."""
    from highhx.actions.catalog import catalog_for
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
    from highhx.commands.computer.main import computer_session
    from highhx.core.errors import UsageError
    from highhx.skills import load
    from highhx.trajectories import TrajectoryStore

    skill = next((s for s in load(app.root) if s.name == name), None)
    if skill is None or not 1 <= index <= len(skill.examples):
        raise UsageError(f"No example {index} in skill {name!r}.")
    example: dict[str, Any] = skill.examples[index - 1]
    surface = (skill.applies_to.get("surfaces") or ["auto"])[0]
    session = computer_session(app)
    executor = ActionExecutor(
        app, session.gate, actor=session.actor, catalog=catalog_for(app), computer=lambda: session
    )
    try:
        result = AgentLoop(
            executor, ScriptedPlanner(example["steps"]), store=TrajectoryStore.for_app(app), agent=f"skill:{name}"
        ).run(AgentTask(str(example["task"]), surface=surface))
    finally:
        executor.close()
        session.close()
    ok = str(result.status) == "completed"
    app.output.emit(
        {"status": str(result.status), "summary": result.summary, "task_id": result.trajectory.id},
        lambda: (app.output.success if ok else app.output.error)(f"{result.status}: {result.summary}"),
    )
    return 0 if ok else 1
