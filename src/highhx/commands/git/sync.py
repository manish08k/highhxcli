"""highhx git sync"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("sync", short_help="Fetch, fast-forward, and optionally push.")
@click.option("--remote", default="origin", show_default=True)
@click.option("--push", is_flag=True, help="Push local commits (asks for confirmation).")
@click.option("--force-push", is_flag=True, help="Force-push with lease (critical; asks you to type 'yes').")
@pass_app
def sync(app: App, remote: str, push: bool, force_push: bool) -> int:
    """Fetch from REMOTE and fast-forward the current branch. Never merges or
    rebases; a diverged branch is reported so you can decide what to do."""
    outcome = app.git.sync(remote=remote, push=push, force_push=force_push)
    app.output.emit(
        outcome.to_dict(),
        lambda: app.output.success(f"{outcome.message} (ahead {outcome.ahead}, behind {outcome.behind})"),
    )
    return 0
