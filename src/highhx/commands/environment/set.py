"""highhx env set"""

from __future__ import annotations

import sys

import click

from highhx.commands import App, pass_app
from highhx.core.errors import UsageError


@click.command("set", short_help="Set a variable in a profile's .env file.")
@click.argument("assignment")
@click.option("--profile", "-p", "profile_name", metavar="NAME", help="Environment profile (default: the active one).")
@click.option(
    "--file", "file_name", metavar="PATH", help="Write to this .env file instead of the profile's first file."
)
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the value from standard input.")
@click.option("--unset", is_flag=True, help="Remove the variable instead.")
@pass_app
def set_var(
    app: App, assignment: str, profile_name: str | None, file_name: str | None, from_stdin: bool, unset: bool
) -> int:
    """Set NAME=VALUE, or just NAME to be prompted without echo (recommended for
    secrets so they don't end up in shell history). New .env files are created
    with owner-only permissions and added to .gitignore."""
    app.require_project()
    manager = app.environment
    name_profile = profile_name or manager.active_profile()
    manager.require_profile(name_profile)
    key, _, value = assignment.partition("=")
    key = key.strip()
    if unset:
        app.engine.approve(
            f"Remove {key} from profile '{name_profile}'", manager.target_risk(name_profile), policy_action="env:set"
        )
        removed = False if app.options.dry_run else manager.unset(key, profile=name_profile, file=file_name)
        app.output.emit(
            {"variable": key, "removed": removed},
            lambda: app.output.success(f"Removed {key}") if removed else app.output.info(f"{key} was not set"),
        )
        return 0
    if "=" not in assignment:
        if from_stdin:
            value = sys.stdin.read().rstrip("\n")
        else:
            prompted = app.prompter.ask(f"Value for {key}", secret=True)
            if prompted is None:
                raise UsageError(
                    f"No value given for {key}.", hint="Use NAME=VALUE, --stdin, or run in an interactive terminal."
                )
            value = prompted
    target = manager.target_file(name_profile, file_name)
    secret = manager.is_secret(key)
    app.engine.approve(
        f"Set {key} in {target.relative_to(app.root).as_posix() if target.is_relative_to(app.root) else target}",
        manager.target_risk(name_profile),
        details=[f"profile: {name_profile}", "value: (hidden)" if secret else f"value: {value}"],
        policy_action="env:set",
    )
    if app.options.dry_run:
        return 0
    path, existed = manager.set(key, value, profile=name_profile, file=file_name)
    rel = path.relative_to(app.root).as_posix() if path.is_relative_to(app.root) else str(path)
    app.output.emit(
        {"variable": key, "file": rel, "updated": existed, "secret": secret},
        lambda: app.output.success(
            f"{'Updated' if existed else 'Set'} {key} in {rel}" + (" (value hidden)" if secret else "")
        ),
    )
    return 0
