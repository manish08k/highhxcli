"""highhx env"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


@click.group(
    "env", cls=DefaultGroup, default_command="show", short_help="Environment variables and profiles (secrets masked)."
)
def env() -> None:
    """Inspect and edit .env-based environment profiles. Secret values are never printed."""


@env.command("show", short_help="Show variables of the active (or given) profile.")
@click.option("--profile", "-p", "profile_name", metavar="NAME", help="Environment profile (default: active).")
@pass_app
def show(app: App, profile_name: str | None) -> int:
    """Show every variable of the active (or given) environment profile with its source file.
    Secret values are always masked; only their length is shown."""
    app.require_project()
    manager = app.environment
    name = profile_name or manager.active_profile()
    manager.require_profile(name)
    rows = manager.describe(name)
    resolved = manager.resolve(name)
    out = app.output

    def render() -> None:
        out.heading(f"Environment: {name}" + (" (protected)" if manager.is_protected(name) else ""))
        if resolved.files_found:
            out.note(f"files: {', '.join(resolved.files_found)}")
        if resolved.files_missing:
            out.note(f"not present: {', '.join(resolved.files_missing)}")
        if rows:
            out.table(
                ["variable", "value", "source", "required"],
                [
                    (
                        r["name"],
                        r["value"] if r["value"] is not None else "(unset)",
                        r["source"] or "-",
                        "yes" if r["required"] else "",
                    )
                    for r in rows
                ],
            )
        else:
            out.info("No variables defined.")

    out.emit(
        {"profile": name, "files": resolved.files_found, "missing_files": resolved.files_missing, "variables": rows},
        render,
    )
    return 0


def _register() -> None:
    from highhx.commands.environment.check import check
    from highhx.commands.environment.diff import diff
    from highhx.commands.environment.profile import profile
    from highhx.commands.environment.set import set_var

    for command in (check, set_var, profile, diff):
        env.add_command(command)


_register()
