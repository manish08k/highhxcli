"""highhx version"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import ValidationError
from highhx.release.versioning import BUMPS, SemVer, suggest_bump


@click.command("version", short_help="Show or bump the project version (semver).")
@click.argument("bump", required=False, metavar="[major|minor|patch|prerelease|X.Y.Z]")
@pass_app
def version(app: App, bump: str | None) -> int:
    """Without an argument, show the current version and the bump suggested by
    commits since the last tag. With major/minor/patch/prerelease or an explicit
    version, update the version files (no commit — see `highhx release`)."""
    manager = app.releases
    current, problems, source = manager.current()
    out = app.output
    if bump is None:
        commits, since = manager.commits_since_last_release()
        suggestion = suggest_bump(commits)
        nxt = str(current.bump(suggestion)) if current else "0.1.0"
        data = {
            "version": str(current) if current else None,
            "source": source,
            "problems": problems,
            "commits_since_release": len(commits),
            "since": since,
            "suggested_bump": suggestion,
            "suggested_version": nxt,
        }

        def render() -> None:
            out.kv(
                {
                    "version": str(current) if current else "(none)",
                    "source": source,
                    "commits since " + (since or "start"): len(commits),
                    "suggested": f"{nxt} ({suggestion})",
                }
            )
            for problem in problems:
                out.warn(problem)

        out.emit(data, render)
        return 0
    if bump in BUMPS:
        if current is None:
            raise ValidationError(
                "No current version found to bump.", hint="Give an explicit version, e.g. `highhx version 0.1.0`."
            )
        target = current.bump(bump)
    else:
        try:
            target = SemVer.parse(bump)
        except ValueError as exc:
            raise click.BadParameter(str(exc)) from exc
    files = manager.bump_files(target)
    out.emit(
        {"from": str(current) if current else None, "to": str(target), "files": files, "dry_run": app.options.dry_run},
        lambda: out.success(f"{'Would set' if app.options.dry_run else 'Set'} version {target} in {', '.join(files)}"),
    )
    return 0
