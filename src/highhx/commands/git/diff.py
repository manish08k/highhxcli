"""highhx git diff"""

from __future__ import annotations

import click
from rich.syntax import Syntax

from highhx.commands import App, pass_app


@click.command("diff", short_help="Show changes (stat or full diff).")
@click.option("--staged", is_flag=True, help="Staged changes.")
@click.option("--stat", "stat_only", is_flag=True, help="Only per-file line counts.")
@click.argument("revision", required=False)
@click.argument("paths", nargs=-1)
@pass_app
def diff(app: App, staged: bool, stat_only: bool, revision: str | None, paths: tuple[str, ...]) -> int:
    """Show the diff of the working tree, staged changes, or against REVISION."""
    repo = app.git.repo
    repo.require()
    changes = repo.diff_stat(staged=staged, revision=revision)
    out = app.output
    if app.options.json:
        out.json(
            {
                "files": [c.to_dict() for c in changes],
                "insertions": sum(c.added or 0 for c in changes),
                "deletions": sum(c.deleted or 0 for c in changes),
            }
        )
        return 0
    if not changes:
        out.info("No changes.")
        return 0
    if stat_only:
        out.table(
            ["file", "+", "-"],
            [(c.path, "bin" if c.binary else c.added, "" if c.binary else c.deleted) for c in changes],
        )
        return 0
    text = repo.diff_text(staged=staged, revision=revision, paths=list(paths) or None)
    out.print(Syntax(text, "diff", theme="ansi_dark", background_color="default", word_wrap=True))
    return 0
