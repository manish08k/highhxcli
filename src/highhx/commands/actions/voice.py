"""highhx voice — local voice for the interactive session (whisper.cpp, set up automatically)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.markup import escape

from highhx.commands import App, pass_app
from highhx.core.errors import UsageError

if TYPE_CHECKING:
    from highhx.voice.mode import VoiceMode


def _mode(app: App) -> VoiceMode:
    from highhx.agent.ui import TerminalUI
    from highhx.voice.mode import VoiceMode

    out = app.output
    ui = TerminalUI(out.console, out.symbols, interactive=app.options.is_interactive())
    return VoiceMode(ui, app.ctx.events)


def _status_data(app: App) -> dict[str, object]:
    from highhx.voice.voice_manager import VoiceManager

    return VoiceManager().status().to_dict()


@click.group("voice", invoke_without_command=True, short_help="The interactive session with local voice on.")
@click.option("--check", is_flag=True, hidden=True, help="Same as `highhx voice status`.")
@click.pass_context
def voice(ctx: click.Context, check: bool) -> int | None:
    """Opens the same session as `highhx`, with voice on: speak, press Enter, and the
    transcript runs through the normal HighhX pipeline — the same resolver (Free) or agent
    (Pro), risk checks, approvals, verification and audit as a typed request.

    Speech-to-text is whisper.cpp, fully local and free (no account, no cloud). The first
    time, HighhX offers to install whisper.cpp and an audio recorder and to download a
    verified model — see docs/VOICE.md.

    \b
    highhx voice            the session with voice on
    highhx voice status     what is installed and ready
    highhx voice setup      install / download what is missing
    highhx voice model      show or switch the whisper model
    highhx voice test       record and transcribe a sample (runs nothing)
    """
    if ctx.invoked_subcommand is not None:
        return None
    app = ctx.find_object(App)
    assert app is not None
    if check or app.options.json:
        return ctx.invoke(status)
    from highhx.agent.launch import interactive_terminal, start_interactive

    if not interactive_terminal(app):
        raise UsageError("Voice needs an interactive terminal.", hint="Run `highhx voice status` to see the setup.")
    return start_interactive(app, voice=True)


@voice.command("status", short_help="whisper.cpp, model, recorder and microphone readiness.")
@pass_app
def status(app: App) -> int:
    """Show whether local voice is ready: whisper.cpp, the model, the recorder, the
    microphone and spoken replies — and the fix for anything missing. Detection only:
    nothing is installed, downloaded or recorded."""
    data = _status_data(app)

    def render() -> None:
        _mode(app).show_status()

    app.output.emit(data, render)
    return 0


@voice.command("setup", short_help="Install whisper.cpp and a recorder, download the model (asks first).")
@pass_app
def setup(app: App) -> int:
    """Install what local voice needs (whisper.cpp and an audio recorder, with Homebrew or the
    system package manager), download and SHA-256-verify the whisper model, and check the
    microphone. Asks before installing or downloading; --yes agrees in advance."""
    from highhx.voice.voice_manager import VoiceManager

    if not app.options.is_interactive() and not app.options.yes:
        raise UsageError(
            "Voice setup installs software and downloads a model, so it asks first.",
            hint="Run it in a terminal, or pass --yes to agree in advance.",
        )
    mode = _mode(app)
    manager = VoiceManager()
    mode._manager = manager
    manager.config.microphone = "unknown"
    result = manager.setup(mode.ui, assume_yes=app.options.yes)
    if result.ready:
        app.output.success("Voice is ready — run `highhx` and type /voice on.")
    return 0 if result.ready else 1


@voice.command("model", short_help="Show or switch the whisper.cpp model.")
@click.argument("name", required=False)
@pass_app
def model(app: App, name: str | None) -> int:
    """Show the whisper.cpp model voice uses and whether it is downloaded, or switch to NAME
    (tiny.en, base.en — the default — or small.en). A new model is downloaded by
    `highhx voice setup` or the next /voice on."""
    from highhx.voice.model_manager import MODELS, model_info
    from highhx.voice.voice_manager import VoiceManager

    manager = VoiceManager()
    if name:
        model_info(name)  # validates
        manager.config.model = name
        manager.save()
    current = manager.models.name
    state = manager.models.status()
    data = {
        "model": current,
        "state": state,
        "path": str(manager.models.path()),
        "available": [{"name": m.name, "size": m.size, "description": m.description} for m in MODELS.values()],
    }

    def render() -> None:
        out = app.output
        out.markup(f"Model  [bold]{escape(current)}[/bold] [dim]({escape(state)})[/dim]")
        out.markup(f"[dim]{escape(str(manager.models.path()))}[/dim]\n")
        for m in MODELS.values():
            mark = "●" if m.name == current else " "
            out.markup(f" {mark} {m.name:<9} {m.size_label:>7}  [dim]{escape(m.description)}[/dim]")
        if state != "ready":
            out.markup("\n[dim]`highhx voice setup` downloads and verifies it.[/dim]")

    app.output.emit(data, render)
    return 0


@voice.command("test", short_help="Record and transcribe a sample — runs nothing.")
@click.option(
    "--file",
    "audio",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Transcribe this 16 kHz WAV file instead of the microphone.",
)
@pass_app
def test(app: App, audio: Path | None) -> int:
    """Check voice end to end: push-to-talk (speak, then Enter), transcribe with whisper.cpp
    and show the transcript and its confidence. Nothing is run. With --file, transcribe a
    WAV file instead of the microphone."""
    from highhx.voice.engines import as_transcript
    from highhx.voice.voice_manager import VoiceManager

    if audio is not None:
        engines = VoiceManager().engines()
        if engines.transcriber is None:
            raise UsageError("Voice is not set up yet.", hint="Run `highhx voice setup`.")
        transcript = as_transcript(engines.transcriber.transcribe(audio))
        data = {"text": transcript.text, "confidence": transcript.confidence, "file": str(audio)}

        def render() -> None:
            conf = "" if transcript.confidence is None else f" [dim](confidence {transcript.confidence:.0%})[/dim]"
            app.output.markup(f"✓ “{escape(transcript.text)}”{conf}")

        app.output.emit(data, render)
        return 0 if transcript.text else 1
    from highhx.agent.launch import interactive_terminal

    if not interactive_terminal(app):
        raise UsageError("The microphone test needs an interactive terminal.", hint="Or pass --file speech.wav.")
    return 0 if _mode(app).test() else 1
