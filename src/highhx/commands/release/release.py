"""highhx release"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.release.versioning import BUMPS


@click.command("release", short_help="Bump version, update changelog, commit and tag.")
@click.argument("bump", required=False, default="auto", metavar="[auto|major|minor|patch|prerelease|X.Y.Z]")
@click.option("--push/--no-push", default=None, help="Push the release commit and tag (default: release.push).")
@click.option("--allow-dirty", is_flag=True, help="Allow uncommitted changes (they are not included).")
@pass_app
def release(app: App, bump: str, push: bool | None, allow_dirty: bool) -> int:
    """Create a release from a clean working tree:

    \b
    1. choose the version (auto = from Conventional Commits)
    2. update version files and the changelog (from real git history)
    3. commit `chore(release): vX.Y.Z` and create an annotated tag
    4. optionally push (asks for confirmation)
    """
    explicit = None if bump in (*BUMPS, "auto") else bump
    plan = app.releases.release(
        bump if explicit is None else "auto", explicit=explicit, push=push, allow_dirty=allow_dirty
    )
    out = app.output

    def render() -> None:
        if app.options.dry_run:
            out.heading(f"Release plan: {plan.tag}")
            out.kv(
                {
                    "current": plan.current or "(none)",
                    "next": plan.next,
                    "bump": plan.bump,
                    "files": ", ".join(plan.files),
                    "commits": len(plan.commits),
                }
            )
            out.plain(plan.changelog)
        else:
            out.success(f"Released {plan.tag} ({plan.bump})")
            out.note("Push with `highhx git sync --push` and publish with `highhx publish` when ready.")

    out.emit(plan.to_dict() | {"dry_run": app.options.dry_run}, render)
    return 0
