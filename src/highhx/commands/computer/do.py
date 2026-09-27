"""highhx do — plain-language requests that HighhX can carry out deterministically (no AI)."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.computer.intents import Intent, parse
from highhx.core.errors import UsageError


def execute_intent(app: App, intent: Intent) -> int:
    """Run a deterministic intent through the normal HighhX commands and the shared gate."""
    if intent.kind == "command":
        from highhx.cli import cli

        result = cli.main(args=list(intent.argv), prog_name="highhx", standalone_mode=False, obj=app)
        return int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
    from highhx.commands.computer.main import computer_session, render_outcome

    session = computer_session(app)
    try:
        if intent.kind in ("navigate", "search"):
            outcome = session.runtime("browser").navigate(intent.url)
        else:
            outcome = session.launch(intent.app)
    finally:
        session.close()
    return render_outcome(app, outcome)


@click.command("do", short_help="Run a plain-language request that maps to a known action (no AI).")
@click.argument("request", nargs=-1, required=True)
@pass_app
def do(app: App, request: tuple[str, ...]) -> int:
    """Understands fixed, unambiguous requests without AI and runs the matching
    HighhX command or automation step:

    \b
      highhx do run the tests
      highhx do "open chrome and search for Adele"
      highhx do open localhost:3000
      highhx do start the dev server

    Requests that need understanding or several adaptive steps ("fix whatever is
    failing") are for the AI agent: `highhx agent "…"` (HighhX Pro).
    """
    text = " ".join(request)
    intent = parse(text)
    if intent is None:
        raise UsageError(
            f"HighhX can't map {text!r} to a fixed action.",
            hint=f'For open-ended tasks use the AI agent (HighhX Pro): highhx agent "{text}"',
        )
    if not app.options.json:
        app.output.note(f"→ {intent.description} (deterministic, no AI)")
    return execute_intent(app, intent)
