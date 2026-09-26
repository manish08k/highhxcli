"""highhx git"""

from __future__ import annotations

import click

from highhx.commands.groups import DefaultGroup


@click.group(
    "git",
    cls=DefaultGroup,
    default_command="status",
    short_help="Safe git operations (status, diff, branch, commit, sync, tag, history).",
)
def git() -> None:
    """Git helpers that never perform destructive operations silently: pushes,
    force operations and branch deletion always require confirmation."""


def _register() -> None:
    from highhx.commands.git.branch import branch
    from highhx.commands.git.commit import commit
    from highhx.commands.git.diff import diff
    from highhx.commands.git.history import history
    from highhx.commands.git.status import status
    from highhx.commands.git.sync import sync
    from highhx.commands.git.tag import tag

    for command in (status, diff, branch, commit, sync, tag, history):
        git.add_command(command)


_register()
