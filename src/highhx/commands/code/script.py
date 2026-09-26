"""highhx script <name>"""

from __future__ import annotations

import click

from highhx.commands import App, exit_code_for, pass_app
from highhx.core.errors import NotFoundError
from highhx.execution.command import CommandSpec
from highhx.testing.runner import with_args
from highhx.utils.validation import did_you_mean


def available_scripts(app: App) -> dict[str, tuple[str, str]]:
    """name -> (command, source). Config scripts override manifest scripts."""
    scripts: dict[str, tuple[str, str]] = {}
    pm = app.profile.package_manager("node") or "npm"
    for manifest in app.profile.manifests:
        for name, body in manifest.scripts.items():
            if manifest.source == "package.json":
                runner = "npm run" if pm == "npm" else "yarn" if pm == "yarn" else f"{pm} run"
                scripts[name] = (f"{runner} {name}", "package.json")
            else:
                scripts[name] = (body, manifest.source)
    for name, body in app.config.scripts.items():
        scripts[name] = (body, "config")
    return dict(sorted(scripts.items()))


@click.command(
    "script",
    short_help="Run a named script (config, package.json, pyproject).",
    context_settings={"ignore_unknown_options": True},
)
@click.argument("name", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_app
def script(app: App, name: str | None, args: tuple[str, ...]) -> int:
    """Run script NAME with optional ARGS; without NAME, list scripts.

    Sources: `scripts:` in .highhx/config.yaml, package.json scripts, and
    [tool.highhx.scripts] in pyproject.toml.
    """
    scripts = available_scripts(app)
    out = app.output
    if name is None:
        out.emit(
            {"scripts": {k: {"command": v[0], "source": v[1]} for k, v in scripts.items()}},
            lambda: (
                out.table(["script", "command", "source"], [(k, v[0], v[1]) for k, v in scripts.items()])
                if scripts
                else out.info("No scripts found.")
            ),
        )
        return 0
    if name not in scripts:
        raise NotFoundError(
            f"Unknown script '{name}'{did_you_mean(name, list(scripts))}.", hint="Run `highhx script` to list scripts."
        )
    command, source = scripts[name]
    full = with_args(command, list(args), "npm" if source == "package.json" else "custom")
    result = app.engine.run(
        CommandSpec(full, cwd=app.root, name=f"script:{name}"),
        action=f"Run script '{name}': {full}",
        policy_action=f"script:{name}",
    )
    app.output.emit(result.to_dict(include_output=False))
    return exit_code_for(result)
