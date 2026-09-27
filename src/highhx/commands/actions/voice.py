"""highhx voice — the interactive session with voice on."""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import UsageError


@click.command("voice", short_help="The interactive session with push-to-talk and spoken replies.")
@click.option("--check", is_flag=True, help="Only report which voice engines are available here.")
@pass_app
def voice(app: App, check: bool) -> int:
    """Opens the same session as `highhx`, with voice on: press Enter on an empty line to
    talk and Enter again to stop; the transcript is shown and confirmed before anything
    runs, and outcomes are spoken. Speech-to-text runs locally (whisper.cpp or Vosk with a
    model you installed) — see docs/VOICE.md. On Free, spoken requests are resolved
    deterministically; on Pro, the AI agent handles them."""
    from highhx.voice.engines import detect

    engines = detect()
    if check or app.options.json:
        data = {**engines.describe(), "can_listen": engines.can_listen, "can_speak": engines.can_speak}
        out = app.output

        def render() -> None:
            out.kv(data)
            out.lines(engines.notes)

        out.emit({**data, "notes": engines.notes}, render)
        return 0
    from highhx.agent.launch import interactive_terminal, start_interactive

    if not interactive_terminal(app):
        raise UsageError("Voice needs an interactive terminal.", hint="Run `highhx voice --check` to see the engines.")
    return start_interactive(app, voice=True)
