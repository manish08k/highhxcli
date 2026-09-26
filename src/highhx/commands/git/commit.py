"""highhx git commit"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("commit", short_help="Create a commit (validates the message).")
@click.option("--message", "-m", required=True, help="Commit message.")
@click.option("--all", "-a", "all_changes", is_flag=True, help="Stage all changes, including untracked files.")
@click.option("--conventional", is_flag=True, help="Require a Conventional Commit message (feat:, fix: …).")
@click.option("--allow-empty", is_flag=True)
@click.argument("paths", nargs=-1)
@pass_app
def commit(
    app: App, message: str, all_changes: bool, conventional: bool, allow_empty: bool, paths: tuple[str, ...]
) -> int:
    """Commit staged changes (or PATHS / --all). Refuses to commit files that
    policies mark as forbidden (e.g. .env, private keys)."""
    sha = app.git.commit(
        message, all_changes=all_changes, paths=list(paths) or None, conventional=conventional, allow_empty=allow_empty
    )
    app.output.emit(
        {"commit": sha, "dry_run": app.options.dry_run},
        lambda: app.output.success(f"Committed {sha}") if not app.options.dry_run else None,
    )
    return 0
