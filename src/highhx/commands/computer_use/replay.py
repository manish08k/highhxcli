"""highhx replay — replay a recorded browser workflow or a past agent task."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("replay", short_help="Replay a browser workflow or a past task (self-healing).")
@click.argument("name")
@click.option("--var", "variables", multiple=True, metavar="NAME=VALUE", help="Values for secret fields (workflows).")
@click.option("--live/--no-live", default=True, help="Show the live dashboard.")
@pass_app
def replay(app: App, name: str, variables: tuple[str, ...], live: bool) -> int:
    """NAME is a recorded browser workflow (`highhx browser workflows`) or a task id
    (`task_…`, see `highhx trajectories`). Targets are grounded again on the current screen —
    never replayed as coordinates — and every action is approved and verified."""
    if not name.startswith("task_"):
        from highhx.commands.computer_use.browser import browser_replay

        result: int = click.get_current_context().invoke(browser_replay, name=name, variables=variables, save_heals=True, live=live)
        return result
    from highhx import computer_use
    from highhx.commands.computer_use.agent import _report

    with computer_use.session(app, live=live and app.output.human, source="replay") as handle:
        outcome = computer_use.replay_trajectory(handle.executor, name)
    return _report(app, outcome)

