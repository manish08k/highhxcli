"""Voice mode: an interface onto the interactive session — not a second product.

    Enter (empty line) → record until Enter → local speech-to-text → "Heard: …" → confirm
      → session.handle(text)  — the same path as typing it: resolver (Free) or agent (Pro),
        the same actions, approvals and audit → a short spoken summary of the outcome

The microphone is only open between those two presses of Enter; nothing listens otherwise.
Voice adds no business logic: it turns speech into the text you would have typed, and the
outcome into a sentence.
"""

from __future__ import annotations

import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from highhx.core.errors import HighhXError
from highhx.voice.engines import VoiceEngines, detect

if TYPE_CHECKING:
    from highhx.agent.ui import TerminalUI
    from highhx.core.events import EventBus


class VoiceMode:
    def __init__(
        self,
        ui: TerminalUI,
        events: EventBus,
        *,
        engines: VoiceEngines | None = None,
        detector: Callable[[], VoiceEngines] = detect,
    ) -> None:
        self.ui = ui
        self.events = events
        self._engines = engines
        self._detector = detector
        self.active = False
        self.muted = False

    @property
    def engines(self) -> VoiceEngines:
        if self._engines is None:
            self._engines = self._detector()
        return self._engines

    # ----------------------------------------------------------- lifecycle
    def enable(self) -> bool:
        engines = self.engines
        if not engines.can_listen and not engines.can_speak:
            self.ui.notice("warn", "Voice is not available on this machine.")
            for note in engines.notes:
                self.ui.print(f"  [dim]• {escape(note)}[/dim]")
            return False
        self.active = True
        self.ui.print(self.banner())
        if not engines.can_listen:
            self.ui.print("[dim]Listening is off (no local speech-to-text); replies are spoken.[/dim]")
            for note in engines.notes:
                self.ui.print(f"  [dim]• {escape(note)}[/dim]")
        self.events.emit("voice.enabled", **engines.describe())
        return True

    def disable(self) -> None:
        self.active = False
        self.interrupt()
        self.ui.print("[dim]Voice off.[/dim]")
        self.events.emit("voice.disabled")

    def banner(self) -> str:
        e = self.engines
        listen = "Enter on an empty line to talk, Enter again to stop" if e.can_listen else "typing"
        speak = "muted" if self.muted else (e.speaker.name if e.speaker else "off")
        return f"[bold magenta]🎙 Voice on[/bold magenta] [dim]— {listen} · replies: {speak} · /voice off[/dim]"

    # ------------------------------------------------------------- listening
    def listen(self, read_line: Callable[[str], str]) -> str | None:
        """Record until Enter, transcribe locally, and let the person confirm or correct the text.
        Returns the text to handle, or None (cancelled / nothing heard / declined)."""
        engines = self.engines
        if not engines.can_listen:
            return None
        assert engines.recorder is not None and engines.transcriber is not None
        self.interrupt()
        with tempfile.TemporaryDirectory(prefix="highhx-voice-") as folder:
            path = Path(folder) / "speech.wav"
            stop = threading.Event()
            failure: list[BaseException] = []

            def record() -> None:
                try:
                    engines.recorder.record(path, stop)  # type: ignore[union-attr]
                except BaseException as exc:  # reported after the thread ends
                    failure.append(exc)

            worker = threading.Thread(target=record, daemon=True)
            self.ui.print("[bold red]● Recording[/bold red] [dim]— press Enter to stop, Ctrl+C to cancel[/dim]")
            worker.start()
            try:
                read_line("")
            except (KeyboardInterrupt, EOFError):
                stop.set()
                worker.join(timeout=10)
                self.ui.print("[dim]Recording cancelled.[/dim]")
                return None
            stop.set()
            worker.join(timeout=10)
            if failure:
                error = failure[0]
                self.ui.notice("error", error.message if isinstance(error, HighhXError) else str(error))
                return None
            try:
                text = engines.transcriber.transcribe(path).strip()
            except HighhXError as exc:
                self.ui.notice("error", exc.message)
                return None
        text = re.sub(r"\s+", " ", text).strip(" .")
        if not text:
            self.ui.notice("info", "Nothing was heard.")
            return None
        self.events.emit("voice.heard", text=text)
        return self.confirm(text, read_line)

    def confirm(self, text: str, read_line: Callable[[str], str]) -> str | None:
        """Speech recognition makes mistakes: nothing runs until the person accepts the text."""
        self.ui.print(f"[bold]Heard:[/bold] “{escape(text)}”")
        try:
            answer = read_line("[bold]Run it?[/bold] [dim]\\[Y/n/e=edit][/dim] ").strip().lower()
            if answer in ("", "y", "yes"):
                return text
            if answer in ("e", "edit"):
                corrected = read_line("[dim]Corrected request:[/dim] ").strip()
                return corrected or None
        except (KeyboardInterrupt, EOFError):
            pass
        self.ui.print("[dim]Not run.[/dim]")
        return None

    # -------------------------------------------------------------- speaking
    def speak(self, text: str) -> None:
        speaker = self.engines.speaker if self.active and not self.muted else None
        if speaker is None or not text.strip():
            return
        try:
            speaker.say(spoken(text))
        except OSError:
            pass

    def interrupt(self) -> None:
        speaker = self._engines.speaker if self._engines is not None else None
        if speaker is not None:
            speaker.stop()

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.interrupt()
        self.ui.print(f"[dim]Voice replies {'muted' if muted else 'on'}.[/dim]")


def spoken(text: str, *, limit: int = 240) -> str:
    """A short, speakable form of an outcome: no Markdown, code or paths spelled out at length."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[`*_#>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s", text)
    out = ""
    for sentence in sentences:
        if len(out) + len(sentence) > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or text[:limit]
