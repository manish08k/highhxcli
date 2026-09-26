"""highhx rollback"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("rollback", short_help="Roll a target back to its previous successful deployment.")
@click.argument("target", required=False)
@click.option("--to", "to_id", metavar="DEPLOYMENT_ID", help="Specific deployment to restore.")
@pass_app
def rollback(app: App, target: str | None, to_id: str | None) -> int:
    """Restore the last successful deployment with a different version (or --to),
    using the target's rollback mechanism, then run its health check."""
    app.require_project()
    outcome = app.deployments.rollback(target, to=to_id)
    out = app.output

    def render() -> None:
        if outcome.dry_run:
            out.info("Dry run: nothing was rolled back.")
        elif outcome.ok and outcome.record:
            out.success(
                f"Rolled back {outcome.record.target} to {outcome.record.version or outcome.record.git_sha} ({outcome.record.id})"
            )
        else:
            out.error("Rollback did not pass its health check.")

    out.emit(outcome.to_dict(), render)
    return 0 if outcome.ok else 1
