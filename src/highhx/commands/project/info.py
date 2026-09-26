"""highhx info"""

from __future__ import annotations

import click

from highhx import __version__
from highhx.commands import App, pass_app


@click.command("info", short_help="Show detected project information.")
@pass_app
def info(app: App) -> int:
    """Everything HighhX detected about this project and its effective commands."""
    profile = app.profile
    data = {
        "highhx_version": __version__,
        "initialized": app.initialized,
        "config": str(app.paths.config_file) if app.initialized else None,
        "profile": profile.to_dict(),
        "effective_commands": app.commands(),
        "manifests": [m.to_dict() for m in profile.manifests],
    }
    out = app.output

    def render() -> None:
        out.heading(f"{profile.name} {profile.version or ''}".strip())
        out.kv(
            {
                "root": str(profile.root),
                "initialized": "yes" if app.initialized else "no",
                "primary stack": profile.primary or "-",
                "stacks": ", ".join(profile.stacks) or "-",
            }
        )
        rows = []
        for group in ("languages", "frameworks", "package_managers", "databases", "containers"):
            for item in getattr(profile, group):
                rows.append((group.replace("_", " ").rstrip("s"), item.name, ", ".join(item.evidence)))
        if rows:
            out.table(["kind", "name", "evidence"], rows, title="Detected")
        if profile.monorepo:
            out.kv({"tool": profile.monorepo.tool, "members": ", ".join(profile.monorepo.members)}, title="Monorepo")
        commands = app.commands()
        if commands:
            configured = app.config.commands
            out.table(
                ["command", "runs", "source"],
                [(k, v, "config" if k in configured else "detected") for k, v in sorted(commands.items())],
                title="Commands",
            )

    out.emit(data, render)
    return 0
