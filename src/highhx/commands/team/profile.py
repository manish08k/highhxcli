"""highhx profile"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.config.loader import available_profiles, read_config_data
from highhx.config.validation import validate_config
from highhx.core.errors import ValidationError
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.validation import is_identifier

PROFILE_TEMPLATE = """# Config profile '{name}': merged over .highhx/config.yaml when you run
#   highhx --config-profile {name} <command>     (or set HIGHHX_PROFILE={name})
# Only list the settings that differ, for example:
# commands:
#   test: pytest -x --maxfail=1
# approvals:
#   auto_approve: normal
"""


@click.group(
    "profile", cls=DefaultGroup, default_command="list", short_help="Config profiles (overlays such as ci or local)."
)
def profile() -> None:
    """Config profiles are YAML overlays in .highhx/profiles/<name>.yaml applied
    with --config-profile NAME or HIGHHX_PROFILE. For environment-variable profiles
    (development/staging/production) see `highhx env profile`."""


@profile.command("list", short_help="List config profiles.")
@pass_app
def list_profiles(app: App) -> int:
    """List config profiles in .highhx/profiles and mark the one selected with --config-profile."""
    app.require_project()
    names = available_profiles(app.root)
    active = app.options.profile
    app.output.emit(
        {"profiles": names, "active": active},
        lambda: (
            app.output.table(["profile", "active"], [(n, "●" if n == active else "") for n in names])
            if names
            else app.output.info("No config profiles. Create one with `highhx profile create ci`.")
        ),
    )
    return 0


@profile.command("create", short_help="Create an empty config profile.")
@click.argument("name")
@pass_app
def create(app: App, name: str) -> int:
    """Create .highhx/profiles/NAME.yaml, an overlay merged over config.yaml when you pass
    --config-profile NAME or set HIGHHX_PROFILE."""
    app.require_project()
    if not is_identifier(name):
        raise ValidationError(f"Invalid profile name '{name}'.")
    path = app.paths.profiles_dir / f"{name}.yaml"
    if path.exists() and not app.options.force:
        raise ValidationError(f"Profile '{name}' already exists.", hint="Use --force to overwrite.")
    if not app.options.dry_run:
        atomic_write_text(path, PROFILE_TEMPLATE.format(name=name))
    app.output.emit({"created": str(path)}, lambda: app.output.success(f"Created {path.relative_to(app.root)}"))
    return 0


@profile.command("show", short_help="Validate a profile and show the merged configuration.")
@click.argument("name")
@pass_app
def show(app: App, name: str) -> int:
    """Show the configuration with profile NAME applied and validate the result."""
    app.require_project()
    import yaml

    data, _sources = read_config_data(app.root, name)
    errors = validate_config(data)

    def render() -> None:
        app.output.plain(yaml.safe_dump(data, sort_keys=False))
        app.output.outcomes((False, e) for e in errors)

    app.output.emit(
        {"profile": name, "valid": not errors, "errors": errors, "config": data},
        render,
    )
    return 3 if errors else 0
