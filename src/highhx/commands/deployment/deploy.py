"""highhx deploy"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


@click.group(
    "deploy",
    cls=DefaultGroup,
    default_command="to",
    short_help="Deploy to a target (preflight, approval, health checks).",
)
def deploy() -> None:
    """Deploy with `highhx deploy [TARGET]`; inspect with `deploy status` / `deploy logs`.

    Targets are defined under deploy.targets in .highhx/config.yaml (types:
    local, docker, ssh, kubernetes, terraform, plugin:<name>).
    """


@deploy.command("to", short_help="Deploy to TARGET (default target if omitted).")
@click.argument("target", required=False)
@click.option(
    "--version", "version_name", metavar="VERSION", help="Version label (default: project version or commit)."
)
@click.option("--skip-preflight", is_flag=True, help="Skip preflight checks (not recommended).")
@pass_app
def deploy_to(app: App, target: str | None, version_name: str | None, skip_preflight: bool) -> int:
    """Run preflight checks, ask for approval (production targets require typing
    the target name), deploy, run health checks and record the deployment.
    Success is only reported when the deployment command and health check pass."""
    app.require_project()
    outcome = app.deployments.deploy(target, version=version_name, skip_preflight=skip_preflight)
    out = app.output

    def render() -> None:
        for check in outcome.preflight:
            out.check(check)
        if outcome.dry_run:
            out.info("Dry run: nothing was deployed.")
            return
        record = outcome.record
        assert record is not None
        if outcome.health and not outcome.health.skipped:
            (out.success if outcome.health.ok else out.error)(
                f"Health check: {outcome.health.message} (attempt {outcome.health.attempts})"
            )
        if outcome.ok:
            out.success(f"Deployed {record.version or '-'} to {record.target} ({record.id})")
        else:
            out.error(f"Deployment {record.id} to {record.target} {record.status}")
            if outcome.rolled_back:
                out.warn("Automatically rolled back to the previous deployment.")

    out.emit(outcome.to_dict(), render)
    return 0 if outcome.ok else 1


def _register() -> None:
    from highhx.commands.deployment.logs import logs
    from highhx.commands.deployment.status import status

    deploy.add_command(status)
    deploy.add_command(logs)


_register()
