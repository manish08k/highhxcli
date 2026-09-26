"""highhx deps update"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("update", short_help="Update dependencies (shows changes first).")
@click.argument("packages", nargs=-1)
@click.option("--manager", "-m", metavar="NAME", help="Package manager to use.")
@pass_app
def update(app: App, packages: tuple[str, ...], manager: str | None) -> int:
    """Update PACKAGES (or everything). The list of outdated packages is shown
    and confirmation is required before a broad update rewrites the lockfile."""
    results = app.dependencies.update(list(packages), manager)
    ok = all(r.ok or r.dry_run for r in results)
    app.output.emit(
        {"ok": ok, "results": [r.to_dict(include_output=False) for r in results]},
        lambda: app.output.success("Dependencies updated") if ok else app.output.error("Update failed"),
    )
    return 0 if ok else 1
