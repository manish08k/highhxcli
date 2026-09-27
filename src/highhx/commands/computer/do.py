"""highhx do — plain-language requests that HighhX carries out deterministically (no AI)."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import UsageError


@click.command("do", short_help="Run a plain-language request that maps to known actions (no AI).")
@click.argument("request", nargs=-1, required=True)
@click.option(
    "--plan", "plan_only", is_flag=True, help="Show the deterministic decision and the action plan; run nothing."
)
@pass_app
def do(app: App, request: tuple[str, ...], plan_only: bool) -> int:
    """HighhX Free's deterministic resolver turns the request into
    a JSON action plan; each step then runs through the action executor
    (risk classification, approval, verification) and the run is traced.

    \b
      highhx do run the tests
      highhx do "open Gmail and search internship"
      highhx do "play lofi on YouTube"
      highhx do --plan --json "open my project and run the tests"

    `highhx "…"` (without `do`) does the same. Requests that need understanding
    ("fix whatever is failing") are for the AI agent: HighhX Pro.
    """
    from highhx.plans.request import run_request

    text = " ".join(request)
    code = run_request(app, text, source="do", plan_only=plan_only)
    if code is None:
        raise UsageError(
            f"HighhX Free can't map {text!r} to known actions — it is an open-ended task.",
            hint=f'Open-ended tasks are for the AI agent (HighhX Pro): highhx agent "{text}"',
        )
    return code
