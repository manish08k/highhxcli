"""highhx git branch"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("branch", short_help="List, create, switch or delete branches.")
@click.argument("name", required=False)
@click.option("--switch", "-s", "switch_to", is_flag=True, help="Switch to NAME (existing branch).")
@click.option("--create", "-c", is_flag=True, help="Create NAME (and switch to it unless --no-switch).")
@click.option("--no-switch", is_flag=True, help="With --create: stay on the current branch.")
@click.option("--from", "start", metavar="REF", help="With --create: start point.")
@click.option("--delete", "-d", is_flag=True, help="Delete NAME (asks for confirmation).")
@pass_app
def branch(
    app: App, name: str | None, switch_to: bool, create: bool, no_switch: bool, start: str | None, delete: bool
) -> int:
    """List branches, or manage branch NAME. Deleting an unmerged branch needs
    --force as well; deleting protected branches is critical and always asks."""
    manager = app.git
    out = app.output
    if name is None:
        manager.repo.require()
        branches = manager.repo.branches()
        out.emit(
            {"branches": [b.to_dict() for b in branches]},
            lambda: out.table(
                ["", "branch", "upstream", "commit", "last commit"],
                [
                    ("*" if b.current else "", b.name, f"{b.upstream} {b.track}".strip() or "-", b.commit, b.date[:10])
                    for b in branches
                ],
            ),
        )
        return 0
    if sum([create, switch_to, delete]) != 1:
        raise click.UsageError("Choose exactly one of --create, --switch or --delete.")
    if create:
        manager.create_branch(name, start=start, switch=not no_switch)
        out.emit({"created": name}, lambda: out.success(f"Created branch {name}"))
    elif switch_to:
        manager.switch(name)
        out.emit({"switched": name}, lambda: out.success(f"Switched to {name}"))
    else:
        manager.delete_branch(name, force=app.options.force)
        out.emit({"deleted": name}, lambda: out.success(f"Deleted branch {name}"))
    return 0
