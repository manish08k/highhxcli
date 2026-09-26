"""highhx hook"""

from __future__ import annotations

import click

from highhx.approvals.risk import RiskLevel
from highhx.automation.hooks import GIT_HOOKS, hook_infos, install_hook, local_hook_scripts, uninstall_hook
from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import ValidationError
from highhx.execution.command import CommandSpec
from highhx.utils.platform import IS_WINDOWS


@click.group("hook", cls=DefaultGroup, default_command="list", short_help="Git hooks that run workflows or commands.")
def hook() -> None:
    """Map git hooks to workflows/commands with `hooks:` in .highhx/config.yaml
    (e.g. `pre-commit: test`) and install them with `highhx hook install`."""


@hook.command("list", short_help="Configured and installed hooks.")
@pass_app
def list_hooks(app: App) -> int:
    """Show git hooks that are configured in `hooks:`, installed in .git/hooks (and whether
    HighhX manages them), or have scripts in .highhx/hooks."""
    infos = hook_infos(app.root, app.config.hooks, app.paths.hooks_dir)
    out = app.output
    out.emit(
        {"hooks": [i.to_dict() for i in infos]},
        lambda: (
            out.table(
                ["hook", "runs", "installed", "managed by highhx", "scripts"],
                [
                    (
                        i.name,
                        i.configured or "-",
                        "yes" if i.installed else "no",
                        "yes" if i.managed else "no",
                        i.script or "-",
                    )
                    for i in infos
                ],
            )
            if infos
            else out.info("No hooks configured. Example config: hooks: {pre-commit: test}")
        ),
    )
    return 0


@hook.command("install", short_help="Install git hook(s) that call HighhX.")
@click.argument("names", nargs=-1)
@pass_app
def install(app: App, names: tuple[str, ...]) -> int:
    """Install git hooks (NAMES, or every hook in `hooks:`) that call `highhx hook run`.
    An existing hook not created by HighhX is only replaced with --force (it is kept as .bak)."""
    app.require_project()
    targets = list(names) or list(app.config.hooks)
    if not targets:
        raise ValidationError(
            "No hooks to install.", hint="Name hooks (e.g. pre-commit) or configure `hooks:` in .highhx/config.yaml."
        )
    app.engine.approve(f"Install git hook(s): {', '.join(targets)}", RiskLevel.NORMAL, policy_action="hook:install")
    installed = (
        [] if app.options.dry_run else [str(install_hook(app.root, n, force=app.options.force).name) for n in targets]
    )
    app.output.emit(
        {"installed": installed or targets, "dry_run": app.options.dry_run},
        lambda: app.output.outcomes((True, f"Installed {n}") for n in installed),
    )
    return 0


@hook.command("uninstall", short_help="Remove HighhX-managed git hook(s).")
@click.argument("names", nargs=-1)
@pass_app
def uninstall(app: App, names: tuple[str, ...]) -> int:
    """Remove HighhX-managed git hooks (NAMES, or all) and restore any hook they replaced."""
    targets = list(names) or list(GIT_HOOKS)
    removed = [n for n in targets if not app.options.dry_run and uninstall_hook(app.root, n)]
    app.output.emit(
        {"removed": removed},
        lambda: app.output.outcomes(((True, f"Removed {n}") for n in removed), empty="No HighhX-managed hooks found."),
    )
    return 0


@hook.command(
    "run",
    short_help="Run what is configured for a hook (called by git).",
    context_settings={"ignore_unknown_options": True},
)
@click.argument("name", type=click.Choice(GIT_HOOKS))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_app
def run_hook(app: App, name: str, args: tuple[str, ...]) -> int:
    """Run what is configured for git hook NAME — the workflow or command in `hooks:` and
    any .highhx/hooks/NAME* scripts. Git calls this; a non-zero exit aborts the git operation."""
    app.require_project()
    ok = True
    target = app.config.hooks.get(name)
    if target:
        if target in app.workflow_loader:
            ok = app.workflows.run(target, env={"HIGHHX_HOOK": name, "HIGHHX_HOOK_ARGS": " ".join(args)}).ok
        else:
            ok = app.engine.run(CommandSpec(target, cwd=app.root, env={"HIGHHX_HOOK": name}, name=f"hook:{name}")).ok
    for script in local_hook_scripts(app.paths.hooks_dir, name):
        if not ok:
            break
        command: list[str] = [str(script), *args]
        if script.suffix == ".py":
            import sys

            command = [sys.executable, str(script), *args]
        elif script.suffix in (".sh", "") and IS_WINDOWS:
            command = ["sh", str(script), *args]
        ok = app.engine.run(
            CommandSpec(command, cwd=app.root, env={"HIGHHX_HOOK": name}, name=f"hook:{script.name}")
        ).ok
    if not ok:
        app.output.error(f"{name} hook failed")
    return 0 if ok else 1
