"""highhx git history"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("history", short_help="Recent commits (with Conventional Commit types).")
@click.option("--limit", "-n", default=20, show_default=True, type=click.IntRange(1, 10000))
@click.argument("revision", required=False)
@click.argument("paths", nargs=-1)
@pass_app
def history(app: App, limit: int, revision: str | None, paths: tuple[str, ...]) -> int:
    """Show recent commits (optionally for REVISION and PATHS) with the Conventional
    Commit type of each, which is what `highhx changelog` groups by."""
    repo = app.git.repo
    repo.require()
    commits = repo.log(limit=limit, revision=revision, paths=list(paths) or None)
    out = app.output
    out.emit(
        {"commits": [c.to_dict() for c in commits]},
        lambda: out.table(
            ["commit", "date", "author", "type", "subject"],
            [
                (
                    c.short,
                    c.date[:10],
                    c.author,
                    (c.conventional.type if c.conventional else "")
                    + ("!" if c.conventional and c.conventional.breaking else ""),
                    c.subject,
                )
                for c in commits
            ],
        ),
    )
    return 0
