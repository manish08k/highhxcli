"""highhx workspace"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import NotFoundError
from highhx.execution.command import join_command
from highhx.project.workspace import workspace_members


@click.group(
    "workspace", cls=DefaultGroup, default_command="list", short_help="Monorepo members; run a command in each."
)
def workspace() -> None:
    """Workspace members come from `workspace.members` in config or are detected
    (pnpm/npm/yarn workspaces, uv, Cargo, Go, Maven, Gradle, apps/* + packages/*)."""


@workspace.command("list", short_help="List workspace members.")
@pass_app
def list_members(app: App) -> int:
    """List workspace members (from workspace.members or detected monorepo tooling) and the
    stack detected in each."""
    members = workspace_members(app.root, app.config.workspace_members)
    from highhx.project.detector import detect_project

    rows = []
    for member in members:
        profile = detect_project(app.root / member.path)
        rows.append({"path": member.path, "name": profile.name, "stack": profile.primary})
    app.output.emit(
        {"members": rows},
        lambda: (
            app.output.table(["member", "name", "stack"], [(r["path"], r["name"], r["stack"] or "-") for r in rows])
            if rows
            else app.output.info("This project has no workspace members.")
        ),
    )
    return 0


@workspace.command(
    "run",
    short_help="Run a command in every member.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--parallel", "-p", is_flag=True, help="Run members in parallel.")
@click.option("--max-parallel", default=4, show_default=True, type=click.IntRange(1, 64))
@click.option("--continue-on-error", is_flag=True, help="Keep going when a member fails.")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@pass_app
def run(app: App, parallel: bool, max_parallel: int, continue_on_error: bool, command: tuple[str, ...]) -> int:
    """Run COMMAND in every workspace member, one after another, or with --parallel. Stops at
    the first failure unless --continue-on-error."""
    members = workspace_members(app.root, app.config.workspace_members)
    if not members:
        raise NotFoundError("No workspace members found.")
    text = command[0] if len(command) == 1 else join_command(command)
    from highhx.workflows.parser import parse_workflow

    steps = [
        {"id": m.path.replace("/", "-").replace(".", "_") or "root", "name": m.path, "run": text, "cwd": m.path}
        for m in members
    ]
    spec = parse_workflow(
        {
            "name": f"workspace: {text}",
            "settings": {"fail_fast": not continue_on_error, "max_parallel": max_parallel if parallel else 1},
            "steps": steps,
        },
        key="workspace-run",
    )
    if not parallel:
        for index in range(1, len(spec.steps)):
            spec.steps[index].depends_on = [spec.steps[index - 1].id]
            if continue_on_error:
                spec.steps[index - 1].continue_on_error = True
    result = app.workflows.run(spec)
    app.output.emit(result.to_dict())
    return 0 if result.ok else 1
