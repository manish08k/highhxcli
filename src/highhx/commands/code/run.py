"""highhx run <workflow>"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


def parse_pairs(values: tuple[str, ...], what: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise click.BadParameter(f"expected NAME=VALUE, got '{value}'", param_hint=what)
        key, val = value.split("=", 1)
        pairs[key.strip()] = val
    return pairs


@click.command("run", short_help="Run a workflow.")
@click.argument("workflow")
@click.option("--input", "-i", "inputs", multiple=True, metavar="NAME=VALUE", help="Workflow input (repeatable).")
@click.option(
    "--env", "-e", "envs", multiple=True, metavar="NAME=VALUE", help="Extra environment variable (repeatable)."
)
@pass_app
def run(app: App, workflow: str, inputs: tuple[str, ...], envs: tuple[str, ...]) -> int:
    """Run WORKFLOW from .highhx/workflows (by file name or `name:`).

    Steps run in parallel where dependencies allow; a step never starts before
    everything in its depends_on succeeded. --dry-run prints the plan.
    """
    result = app.workflows.run(workflow, inputs=parse_pairs(inputs, "--input"), env=parse_pairs(envs, "--env"))
    app.output.emit(result.to_dict())
    return 0 if result.ok else (130 if result.status == "cancelled" else 124 if result.status == "timeout" else 1)
