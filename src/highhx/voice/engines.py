"""Voice engines: local programs that record, transcribe and speak.

Nothing here talks to a network service or a language model. Recording and speech use the
operating system's tools; speech-to-text uses a *local* engine the user installed and chose
(whisper.cpp's CLI or Vosk, each with a model file on disk). Which engines are available is
detected, never assumed, and each can be selected or disabled with environment variables:

    HIGHHX_VOICE_RECORDER   rec | ffmpeg | arecord | off
    HIGHHX_VOICE_STT        whisper | vosk | off
    HIGHHX_WHISPER_MODEL    path to a whisper.cpp model (e.g. ggml-base.en.bin)
    HIGHHX_VOSK_MODEL       path to a Vosk model directory
    HIGHHX_VOICE_TTS        say | espeak | spd-say | sapi | off
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess  # nosec B404 - fixed, argument-list invocations of local audio tools; no shell
import sys
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from highhx.core.errors import IntegrationError

SAMPLE_RATE = 16000
MAX_SPOKEN_CHARS = 400


class Recorder(Protocol):
    name: str

    def record(self, path: Path, stop: threading.Event) -> None:
        """Record 16 kHz mono WAV into ``path`` until ``stop`` is set."""
        ...


class Transcriber(Protocol):
    name: str

    def transcribe(self, path: Path) -> str: ...


class Speaker(Protocol):
    name: str

    def say(self, text: str) -> None: ...

    def stop(self) -> None: ...


# ------------------------------------------------------------------ recording
@dataclass
class ProcessRecorder:
    """A command-line recorder that writes a WAV file and stops cleanly on SIGINT."""

    name: str
    argv: list[str]

    def record(self, path: Path, stop: threading.Event) -> None:
        command = [part.replace("{out}", str(path)) for part in self.argv]
        process = subprocess.Popen(  # nosec B603 - argv built from a fixed template and a temp path
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        try:
            while not stop.wait(0.05):
                if process.poll() is not None:
                    break
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)  # lets the tool finalise the WAV header
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if not path.exists() or path.stat().st_size <= 44:
            error = process.stderr.read().decode(errors="replace").strip() if process.stderr else ""
            raise IntegrationError(f"{self.name} recorded no audio.", hint=error[-300:] or "Check microphone access.")


def _recorders() -> list[ProcessRecorder]:
    found: list[ProcessRecorder] = []
    if shutil.which("rec"):
        found.append(ProcessRecorder("rec", ["rec", "-q", "-c", "1", "-r", str(SAMPLE_RATE), "-b", "16", "{out}"]))
    if shutil.which("ffmpeg"):
        source = (
            ["-f", "avfoundation", "-i", ":0"]
            if sys.platform == "darwin"
            else ["-f", "alsa", "-i", "default"]
            if sys.platform.startswith("linux")
            else None
        )
        if source is not None:
            found.append(
                ProcessRecorder(
                    "ffmpeg",
                    ["ffmpeg", "-loglevel", "error", "-y", *source, "-ac", "1", "-ar", str(SAMPLE_RATE), "{out}"],
                )
            )
    if shutil.which("arecord"):
        found.append(
            ProcessRecorder("arecord", ["arecord", "-q", "-f", "S16_LE", "-c", "1", "-r", str(SAMPLE_RATE), "{out}"])
        )
    return found


# -------------------------------------------------------------- speech-to-text
@dataclass
class WhisperCppTranscriber:
    """whisper.cpp's command-line tool with a local model file."""

    binary: str
    model: Path
    name: str = "whisper.cpp"
    language: str = "en"

    def transcribe(self, path: Path) -> str:
        done = subprocess.run(  # nosec B603 - fixed argv
            [self.binary, "-m", str(self.model), "-f", str(path), "-l", self.language, "-nt", "-np"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            raise IntegrationError("Speech recognition failed.", hint=done.stderr.strip()[-300:] or None)
        return " ".join(done.stdout.split())


@dataclass
class VoskTranscriber:
    """Vosk (offline speech recognition) with a local model directory."""

    model: Path
    name: str = "vosk"

    def transcribe(self, path: Path) -> str:
        import json

        import vosk

        recognizer = vosk.KaldiRecognizer(vosk.Model(str(self.model)), SAMPLE_RATE)
        with wave.open(str(path), "rb") as audio:
            while True:
                frames = audio.readframes(4000)
                if not frames:
                    break
                recognizer.AcceptWaveform(frames)
        return str(json.loads(recognizer.FinalResult()).get("text", "")).strip()


def _transcribers() -> tuple[list[Transcriber], list[str]]:
    found: list[Transcriber] = []
    notes: list[str] = []
    binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    model = os.environ.get("HIGHHX_WHISPER_MODEL")
    if binary and model and Path(model).is_file():
        found.append(WhisperCppTranscriber(binary, Path(model)))
    elif binary:
        notes.append("whisper.cpp is installed: set HIGHHX_WHISPER_MODEL to a model file (e.g. ggml-base.en.bin)")
    vosk_model = os.environ.get("HIGHHX_VOSK_MODEL")
    if vosk_model and Path(vosk_model).is_dir():
        try:
            import vosk  # noqa: F401
        except ImportError:
            notes.append("HIGHHX_VOSK_MODEL is set but the `vosk` package is not installed")
        else:
            found.append(VoskTranscriber(Path(vosk_model)))
    return found, notes


# ------------------------------------------------------------------- speaking
@dataclass
class ProcessSpeaker:
    name: str
    argv: list[str]
    _process: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    def say(self, text: str) -> None:
        text = " ".join(text.split())[:MAX_SPOKEN_CHARS]
        if not text:
            return
        self.stop()
        self._process = subprocess.Popen(  # nosec B603 - fixed argv; text is a single argument
            [part.replace("{text}", text) for part in self.argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None

    def wait(self, timeout: float = 30) -> None:
        if self._process is not None:
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop()


def _speakers() -> list[ProcessSpeaker]:
    found: list[ProcessSpeaker] = []
    if sys.platform == "darwin" and shutil.which("say"):
        found.append(ProcessSpeaker("say", ["say", "-r", "205", "{text}"]))
    for binary in ("espeak-ng", "espeak"):
        if shutil.which(binary):
            found.append(ProcessSpeaker("espeak", [binary, "{text}"]))
            break
    if shutil.which("spd-say"):
        found.append(ProcessSpeaker("spd-say", ["spd-say", "-w", "{text}"]))
    if sys.platform == "win32" and shutil.which("powershell"):
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($args[0])"
        )
        found.append(ProcessSpeaker("sapi", ["powershell", "-NoProfile", "-Command", script, "{text}"]))
    return found


# ------------------------------------------------------------------ detection
@dataclass
class VoiceEngines:
    recorder: Recorder | None = None
    transcriber: Transcriber | None = None
    speaker: Speaker | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def can_listen(self) -> bool:
        return self.recorder is not None and self.transcriber is not None

    @property
    def can_speak(self) -> bool:
        return self.speaker is not None

    def describe(self) -> dict[str, str]:
        return {
            "recorder": self.recorder.name if self.recorder else "none",
            "speech-to-text": self.transcriber.name if self.transcriber else "none",
            "speech": self.speaker.name if self.speaker else "none",
        }


def _choose(options: list[object], choice: str | None) -> object | None:
    if choice == "off":
        return None
    if choice:
        return next((o for o in options if getattr(o, "name", "") == choice), None)
    return options[0] if options else None


def detect() -> VoiceEngines:
    """The engines available on this machine (honouring the HIGHHX_VOICE_* choices)."""
    engines = VoiceEngines()
    recorders = _recorders()
    engines.recorder = _choose(list(recorders), os.environ.get("HIGHHX_VOICE_RECORDER"))  # type: ignore[assignment]
    if not recorders:
        engines.notes.append("no recorder found — install sox (`rec`) or ffmpeg")
    transcribers, notes = _transcribers()
    engines.notes += notes
    engines.transcriber = _choose(list(transcribers), os.environ.get("HIGHHX_VOICE_STT"))  # type: ignore[assignment]
    if not transcribers and not notes:
        engines.notes.append(
            "no local speech-to-text engine — install whisper.cpp and set HIGHHX_WHISPER_MODEL, "
            "or `pip install vosk` and set HIGHHX_VOSK_MODEL"
        )
    speakers = _speakers()
    engines.speaker = _choose(list(speakers), os.environ.get("HIGHHX_VOICE_TTS"))  # type: ignore[assignment]
    return engines
