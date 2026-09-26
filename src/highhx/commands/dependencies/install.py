"""highhx deps install"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("install", short_help="Install dependencies (lockfile-respecting).")
@click.option("--manager", "-m", metavar="NAME", help="Only this package manager / ecosystem.")
@pass_app
def install(app: App, manager: str | None) -> int:
    """Install dependencies exactly as declared: `npm ci` when a lockfile exists,
    `uv sync`, `poetry install`, `flutter pub get` … Lockfiles are not upgraded."""
    results = app.dependencies.install(manager)
    ok = all(r.ok or r.dry_run for r in results)
    out = app.output
    out.emit(
        {"ok": ok, "results": [r.to_dict(include_output=False) for r in results]},
        lambda: out.outcomes((r.ok, r.command) for r in results if not r.dry_run),
    )
    return 0 if ok else 1
