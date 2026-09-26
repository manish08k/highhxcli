"""highhx dev"""

from __future__ import annotations

import click

from highhx.commands import App, exit_code_for, pass_app
from highhx.core.errors import NotFoundError
from highhx.execution.command import CommandSpec


@click.command("dev", short_help="Start the development environment.")
@click.option("--no-workflow", is_flag=True, help="Ignore .highhx/workflows/dev.yaml and run commands.dev directly.")
@pass_app
def dev(app: App, no_workflow: bool) -> int:
    """Run the `dev` workflow if the project has one, otherwise the dev command
    (commands.dev or the detected one) interactively in the foreground."""
    if not no_workflow and app.initialized and "dev" in app.workflow_loader:
        workflow_result = app.workflows.run("dev")
        app.output.emit(workflow_result.to_dict())
        return 0 if workflow_result.ok else 1
    command = app.commands().get("dev") or app.commands().get("start")
    if not command:
        raise NotFoundError("No dev command configured or detected.", hint="Set commands.dev in .highhx/config.yaml.")
    app.output.info(f"Starting: {command}  (Ctrl+C to stop)")
    result = app.engine.run(
        CommandSpec(command, cwd=app.root, interactive=not app.options.json, name="dev"),
        action=f"Start dev server: {command}",
        policy_action="dev",
    )
    app.output.emit(result.to_dict(include_output=False))
    return 0 if result.status == "cancelled" else exit_code_for(result)
