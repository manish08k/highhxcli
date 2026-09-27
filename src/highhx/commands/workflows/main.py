"""highhx workflow"""

from __future__ import annotations

import click

from highhx.commands.groups import DefaultGroup


@click.group(
    "workflow",
    cls=DefaultGroup,
    default_command="list",
    short_help="Create, run, inspect, resume and cancel workflows.",
)
def workflow() -> None:
    """Workflows are YAML files in .highhx/workflows (see docs/AUTOMATION.md): commands,
    HighhX actions and other workflows as steps, with dependencies, conditions, retries,
    approvals and rollback. Run one with `highhx workflow run <name>` (or `highhx run`)."""


def _register() -> None:
    from highhx.commands.workflows.create import create
    from highhx.commands.workflows.execution import (
        cancel_workflow,
        inspect_workflow,
        list_runs,
        resume_workflow,
        run_workflow,
    )
    from highhx.commands.workflows.graph import graph
    from highhx.commands.workflows.list import list_workflows
    from highhx.commands.workflows.validate import validate

    for command in (
        list_workflows,
        validate,
        create,
        graph,
        run_workflow,
        inspect_workflow,
        list_runs,
        resume_workflow,
        cancel_workflow,
    ):
        workflow.add_command(command)


_register()
