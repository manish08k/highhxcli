"""highhx check"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.workflows.parser import parse_workflow


@click.command("check", short_help="Run lint, type-check and tests in parallel.")
@pass_app
def check(app: App) -> int:
    """Run the `check` workflow if defined; otherwise run the project's lint,
    typecheck and test commands in parallel (continuing past failures)."""
    if app.initialized and "check" in app.workflow_loader:
        result = app.workflows.run("check")
    else:
        commands = app.commands()
        steps = [{"id": name, "run": commands[name]} for name in ("lint", "typecheck", "test") if commands.get(name)]
        if not steps:
            raise NotFoundError(
                "No lint, typecheck or test command found.",
                hint="Configure commands.lint / commands.test in .highhx/config.yaml.",
            )
        spec = parse_workflow(
            {"name": "check", "settings": {"fail_fast": False, "max_parallel": 3}, "steps": steps}, key="check"
        )
        result = app.workflows.run(spec)
    app.output.emit(result.to_dict())
    return 0 if result.ok else 1
