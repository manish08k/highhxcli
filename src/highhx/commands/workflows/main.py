"""highhx workflow"""

from __future__ import annotations

import click

from highhx.commands.groups import DefaultGroup


@click.group(
    "workflow", cls=DefaultGroup, default_command="list", short_help="List, validate, create and graph workflows."
)
def workflow() -> None:
    """Workflows are YAML files in .highhx/workflows (see docs/workflows.md).
    Run one with `highhx run <name>`."""


def _register() -> None:
    from highhx.commands.workflows.create import create
    from highhx.commands.workflows.graph import graph
    from highhx.commands.workflows.list import list_workflows
    from highhx.commands.workflows.validate import validate

    for command in (list_workflows, validate, create, graph):
        workflow.add_command(command)


_register()
