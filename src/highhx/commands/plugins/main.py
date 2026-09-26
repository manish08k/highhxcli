"""highhx plugin"""

from __future__ import annotations

import click

from highhx.approvals.risk import RiskLevel
from highhx.commands import App, exit_code_for, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.execution.command import CommandSpec, join_command
from highhx.plugins.sandbox import command_environment


def plugin_click_command(app: App, name: str) -> click.Command | None:
    """A click command contributed by a plugin (code or declarative), if any."""
    registry = app.plugins
    if name in registry.click_commands:
        return registry.click_commands[name][1]
    if name not in registry.declarative_commands:
        return None
    manifest, spec = registry.declarative_commands[name]

    @click.command(
        name,
        help=f"{spec.description or spec.run}\n\nProvided by plugin '{manifest.name}' {manifest.version}.",
        context_settings={"ignore_unknown_options": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @pass_app
    def _command(app: App, args: tuple[str, ...]) -> int:
        command = spec.run + ("" if not args else " " + join_command(args))
        env = command_environment(manifest)
        result = app.engine.run(
            CommandSpec(command, cwd=app.root, env_base=env, name=f"plugin:{manifest.name}:{name}"),
            action=f"Run plugin command '{name}' ({manifest.name}): {command}",
            risk=RiskLevel.parse(spec.risk),
            policy_action=f"plugin:{manifest.name}:{name}",
        )
        return exit_code_for(result)

    return _command


@click.group("plugin", cls=DefaultGroup, default_command="list", short_help="Install and manage plugins.")
def plugin() -> None:
    """Plugins add commands, workflows, templates, detectors and deployment
    backends. Code plugins only run when plugins.allow_code is true and their
    files match the hash recorded at installation (see docs/plugins.md)."""


def _register() -> None:
    from highhx.commands.plugins.install import install
    from highhx.commands.plugins.list import list_plugins
    from highhx.commands.plugins.remove import remove
    from highhx.commands.plugins.search import search
    from highhx.commands.plugins.trust import trust
    from highhx.commands.plugins.update import update

    for command in (list_plugins, install, remove, update, search, trust):
        plugin.add_command(command)


_register()
