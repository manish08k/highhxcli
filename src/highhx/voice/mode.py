"""Voice mode: an interface onto the interactive session — not a second product.

    /voice on → setup if needed → 🎙 Listening… (push-to-talk: Enter stops) → whisper.cpp
      → ✓ "transcript" → session.handle(text) — the same path as typing it: resolver (Free) or
        agent (Pro), the same action plan, risk, approvals, executor, verification and audit
      → a short spoken summary of the outcome

The microphone is only open while a recording runs; nothing listens otherwise. Voice adds no
business logic and no safety exceptions: it turns speech into the text you would have typed.
Transcripts whisper.cpp is unsure of are shown for confirmation before anything runs.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.table import Table

from highhx.core.errors import HighhXError
from highhx.voice.engines import Transcript, VoiceEngines, VoiceError, as_transcript
from highhx.voice.recorder import check_audio
from highhx.voice.whisper import clean

if TYPE_CHECKING:
    from highhx.agent.ui import TerminalUI
    from highhx.core.events import EventBus
    from highhx.voice.voice_manager import VoiceManager

LOW_CONFIDENCE = 0.6
"""Below this mean token probability a transcript is confirmed before it runs."""


class VoiceMode:
    def __init__(
        self,
        ui: TerminalUI,
        events: EventBus,
        *,
        engines: VoiceEngines | None = None,
        manager: VoiceManager | None = None,
    ) -> None:
        self.ui = ui
        self.events = events
        self._engines = engines
        self._manager = manager
        self.active = False
        self.muted = False if manager is None else not manager.config.replies
        self.typed_instead: str | None = None
        """A line typed (instead of a bare Enter) while recording: the audio is discarded and the
        session handles this text as a typed request — ``/voice status`` must never become speech."""

    @property
    def manager(self) -> VoiceManager:
        if self._manager is None:
            from highhx.voice.voice_manager import VoiceManager

            self._manager = VoiceManager()
            self.muted = not self._manager.config.replies
        return self._manager

    @property
    def engines(self) -> VoiceEngines:
        if self._engines is None:
            self._engines = self.manager.engines()
        return self._engines

    def _read(self, prompt: str) -> str:
        return self.ui.read_line(prompt)

    # ----------------------------------------------------------- lifecycle
    def ensure_ready(self) -> bool:
        """Detect; run the first-run setup for whatever is missing. True when listening works."""
        if self._engines is not None and self._engines.can_listen:
            return True
        status = self.manager.status()
        if not status.ready:
            self.manager.setup(self.ui)
        self._engines = self.manager.engines()
        if self._engines.can_listen:
            return True
        status = self.manager.status()
        if status.missing:
            self.ui.print("[warn]⚠ Voice input is not ready.[/warn]")
            for component in status.missing:
                fix = f" — run {component.fix}" if component.fix else ""
                self.ui.print(f"  [dim]• {escape(component.name)}: {escape(component.detail)}{escape(fix)}[/dim]")
        return False

    def enable(self) -> bool:
        if not self.ensure_ready():
            self.ui.print("[dim]HighhX keeps working normally with typed requests.[/dim]")
            return False
        if self.active:
            self.ui.print("[dim]🎙 Voice is already on — press Enter on an empty line to talk.[/dim]")
            return True
        self.active = True
        self.ui.print(self.banner())
        self.events.emit("voice.enabled", **self.engines.describe())
        return True

    def disable(self) -> None:
        self.active = False
        self.interrupt()
        self.ui.print("[dim]🎙 Voice off.[/dim]")
        self.events.emit("voice.disabled")

    def banner(self) -> str:
        e = self.engines
        model = f" [dim]({escape(e.model)})[/dim]" if e.model else ""
        microphone = "ready" if self._manager is None else self.manager.config.microphone
        microphone = "ready" if microphone == "unknown" else microphone
        replies = "muted" if self.muted else ("enabled" if e.speaker else "unavailable")
        return (
            "\n[bold magenta]🎙 Voice on[/bold magenta]\n"
            f"Speech-to-text: {escape(e.transcriber.name if e.transcriber else 'none')}{model}\n"
            f"Microphone: {escape(microphone)}\n"
            f"Voice replies: {replies}\n"
            "[dim]Push-to-talk: press Enter on an empty line to talk · /voice off[/dim]"
        )

    # ------------------------------------------------------------- listening
    def capture(self, *, title: str = "🎙 Listening...") -> Transcript | None:
        """Push-to-talk: record until Enter, transcribe locally. None when cancelled or nothing heard."""
        if not self.ensure_ready():
            return None
        engines = self.engines
        recorder, transcriber = engines.recorder, engines.transcriber
        assert recorder is not None and transcriber is not None
        self.interrupt()
        with tempfile.TemporaryDirectory(prefix="highhx-voice-") as folder:
            path = Path(folder) / "speech.wav"
            stop = threading.Event()
            failure: list[BaseException] = []

            def record() -> None:
                try:
                    recorder.record(path, stop)
                except BaseException as exc:  # reported after the thread ends
                    failure.append(exc)

            worker = threading.Thread(target=record, daemon=True)
            self.ui.print(
                f"[bold red]{title}[/bold red]\nSpeak now. [dim]Press Enter when you're done · Ctrl+C cancels[/dim]"
            )
            worker.start()
            try:
                line = self._read("").strip()
            except (KeyboardInterrupt, EOFError):
                stop.set()
                worker.join(timeout=10)
                self.ui.print("[dim]Recording cancelled.[/dim]")
                return None
            stop.set()
            worker.join(timeout=10)
            if line:
                self.typed_instead = line  # typed, not spoken: the recording is not transcribed
                self.ui.print("[dim]Recording discarded — handling what you typed.[/dim]")
                return None
            try:
                if failure:
                    raise failure[0]
                level = check_audio(path)
            except VoiceError as exc:
                self._microphone(ok=exc.problem not in ("microphone-denied", "microphone-unavailable"))
                self._report(exc)
                return None
            except HighhXError as exc:
                self._report(exc)
                return None
            self._microphone(ok=True)
            if level.seconds > 0 and level.quiet:
                self.ui.notice("info", "Nothing was heard — try again a little closer to the microphone.")
                return None
            self.ui.print("[magenta]◉ Transcribing...[/magenta]")
            try:
                transcript = as_transcript(transcriber.transcribe(path))
            except HighhXError as exc:
                self._report(exc)
                return None
            except OSError as exc:
                self._report(VoiceError("transcription-failed", "Transcription failed.", hint=str(exc)))
                return None
        text = clean(transcript.text)
        if not text:
            self.ui.notice("info", "Nothing was heard.")
            return None
        return Transcript(text, transcript.confidence)

    def listen(self) -> str | None:
        """Capture one spoken request. Returns the text to handle — through the session's normal
        pipeline — or None (cancelled, nothing heard, or a doubtful transcript was declined)."""
        transcript = self.capture()
        if transcript is None:
            return None
        self.events.emit("voice.heard", text=transcript.text, confidence=transcript.confidence)
        if self.needs_confirmation(transcript):
            return self.confirm(transcript)
        self.ui.print(f"[ok]✓[/ok] “{escape(transcript.text)}”")
        return transcript.text

    def needs_confirmation(self, transcript: Transcript) -> bool:
        always = self._manager is not None and self.manager.config.confirm == "always"
        return always or transcript.confidence is None or transcript.confidence < LOW_CONFIDENCE

    def confirm(self, transcript: Transcript) -> str | None:
        """A transcript whisper.cpp is unsure of: nothing runs until the person accepts or corrects it."""
        note = "" if transcript.confidence is None else f" [dim](confidence {transcript.confidence:.0%})[/dim]"
        self.ui.print(f"[bold]Heard:[/bold] “{escape(transcript.text)}”{note}")
        try:
            answer = self._read("[bold]Run it?[/bold] [dim]\\[y/N/e=edit][/dim] ").strip().lower()
            if answer in ("y", "yes"):
                return transcript.text
            if answer in ("e", "edit"):
                corrected = self._read("[dim]Corrected request:[/dim] ").strip()
                return corrected or None
        except (KeyboardInterrupt, EOFError):
            pass
        self.ui.print("[dim]Not run.[/dim]")
        return None

    def test(self) -> bool:
        """/voice test: record, transcribe and show the result — runs nothing."""
        started = time.monotonic()
        transcript = self.capture(title="🎙 Voice test — say something")
        if transcript is None:
            return False
        seconds = time.monotonic() - started
        confidence = "" if transcript.confidence is None else f" · confidence {transcript.confidence:.0%}"
        self.ui.print(f"[ok]✓[/ok] “{escape(transcript.text)}”")
        self.ui.print(f"[dim]whisper.cpp{escape(confidence)} · {seconds:.1f}s · nothing was run[/dim]")
        speaker = self.engines.speaker
        if speaker is not None and not self.muted:
            try:
                speaker.say("Voice test complete.")
            except OSError:
                pass
        return True

    # ---------------------------------------------------------------- status
    def status_table(self) -> Table:
        table = Table.grid(padding=(0, 4))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        replies = "disabled" if self.muted else "enabled"
        if self._manager is None and self._engines is not None:
            e = self._engines
            rows = [
                ("Status", "ON" if self.active else "OFF"),
                ("Speech-to-text", e.transcriber.name if e.transcriber else "unavailable"),
                ("Model", e.model or "unknown"),
                ("Recorder", f"ready ({e.recorder.name})" if e.recorder else "unavailable"),
                ("Voice replies", replies if e.speaker else "unavailable"),
                ("Push-to-talk", "ready" if e.can_listen else "unavailable"),
            ]
        else:
            s = self.manager.status()
            microphone = {"ready": "ready", "unknown": "not checked yet"}.get(
                s.microphone, "unavailable (access denied)"
            )
            rows = [
                ("Status", "ON" if self.active else "OFF"),
                ("Speech-to-text", "whisper.cpp" if s.whisper else "whisper.cpp — not installed"),
                ("Model", s.model if s.model_state == "ready" else f"{s.model} ({s.model_state})"),
                ("Microphone", microphone),
                ("Recorder", f"ready ({s.recorder})" if s.recorder else "unavailable"),
                ("Voice replies", replies if s.speaker else "unavailable"),
                ("Push-to-talk", "ready" if s.ready else "unavailable"),
                ("Setup", "ready" if s.ready else "incomplete"),
            ]
        for key, value in rows:
            table.add_row(key, escape(value))
        return table

    def show_status(self) -> None:
        self.ui.print("\n[bold magenta]🎙 HighhX Voice[/bold magenta]\n")
        self.ui.print(self.status_table())
        if self._manager is not None or self._engines is None:
            missing = self.manager.status().missing
            if missing:
                self.ui.print("\n[warn]Missing[/warn]")
                for c in missing:
                    fix = f" → {c.fix}" if c.fix else ""
                    self.ui.print(f"  [dim]• {escape(c.name)}: {escape(c.detail)}{escape(fix)}[/dim]")
                self.ui.print("\n[dim]/voice setup installs and downloads what is missing (it asks first).[/dim]")
        elif self._engines is not None:
            for note in self._engines.notes:
                self.ui.print(f"  [dim]• {escape(note)}[/dim]")

    def setup(self) -> bool:
        """/voice setup: run the setup (and a microphone check) even when things look ready."""
        self.manager.config.microphone = "unknown"
        status = self.manager.setup(self.ui)
        self._engines = None
        if status.ready:
            self.ui.print("[ok]✓ Voice is ready.[/ok] [dim]/voice on to start · /voice test to try it[/dim]")
        return status.ready

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
        if self._manager is not None:
            self._manager.config.replies = not muted
            self._manager.save()
        self.ui.print(f"[dim]Voice replies {'muted' if muted else 'on'}.[/dim]")

    # ---------------------------------------------------------------- errors
    def _report(self, exc: HighhXError) -> None:
        self.ui.notice("warn", exc.message)
        if exc.hint:
            self.ui.print(f"\n{escape(exc.hint)}\n")
        if isinstance(exc, VoiceError) and exc.problem.startswith("microphone"):
            self.ui.print("[dim]Then try again: press Enter on an empty line, or /voice test.[/dim]")

    def _microphone(self, *, ok: bool) -> None:
        if self._manager is not None:
            self._manager.record_microphone(ok)


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
