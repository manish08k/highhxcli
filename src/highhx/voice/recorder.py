"""Microphone recording with a local command-line recorder.

The microphone is open only while a recording runs: between the person starting push-to-talk
and pressing Enter (or a short microphone check during setup). A recording that is entirely
digital silence is how macOS reports a terminal that was denied microphone access, so it is
reported as that rather than as "nothing was heard".
"""

from __future__ import annotations

import array
import os
import shutil
import signal
import subprocess  # nosec B404 - fixed, argument-list invocations of local audio tools; no shell
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from highhx.voice.engines import SAMPLE_RATE, VoiceError
from highhx.voice.platform import VoicePlatform, Which

MAX_SECONDS = 60.0
"""A recording stops by itself after this long, even if Enter is never pressed."""
QUIET_PEAK = 60
"""Peak sample amplitude (of 32767) below which a recording is treated as containing nothing."""

MICROPHONE_HELP = {
    "macos": (
        "Enable microphone access for your terminal (Terminal, iTerm, VS Code …) in:\n"
        "System Settings → Privacy & Security → Microphone — then quit and reopen that app."
    ),
    "linux": "Check that a microphone is connected and not muted (`alsamixer`, or your desktop's sound settings).",
}


@dataclass
class ProcessRecorder:
    """A command-line recorder that writes a WAV file and stops cleanly on SIGINT."""

    name: str
    argv: list[str]
    max_seconds: float = MAX_SECONDS

    def record(self, path: Path, stop: object) -> None:
        assert isinstance(stop, threading.Event)
        command = [part.replace("{out}", str(path)) for part in self.argv]
        try:
            process = subprocess.Popen(  # nosec B603 - argv built from a fixed template and a temp path
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise VoiceError(
                "recorder-unavailable", f"The {self.name} recorder could not start.", hint=str(exc)
            ) from None
        waited = 0.0
        try:
            while not stop.wait(0.05):
                waited += 0.05
                if process.poll() is not None or waited >= self.max_seconds:
                    break
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)  # lets the tool finalise the WAV header
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        error = process.stderr.read().decode(errors="replace").strip() if process.stderr else ""
        if not path.exists() or path.stat().st_size <= 44:
            raise VoiceError(
                "microphone-unavailable",
                "Microphone access is unavailable.",
                hint=MICROPHONE_HELP.get(_system(), error[-300:] or None),
            )


def _system() -> str:
    return "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform


def find_recorders(platform: VoicePlatform, which: Which = shutil.which) -> list[ProcessRecorder]:
    """Recorders on this machine, best first."""
    found: list[ProcessRecorder] = []
    rate = str(SAMPLE_RATE)
    if rec := which("rec"):
        found.append(ProcessRecorder("rec", [rec, "-q", "-c", "1", "-r", rate, "-b", "16", "{out}"]))
    if (ffmpeg := which("ffmpeg")) and platform.system in ("macos", "linux"):
        source = ["-f", "avfoundation", "-i", ":0"] if platform.system == "macos" else ["-f", "alsa", "-i", "default"]
        found.append(
            ProcessRecorder("ffmpeg", [ffmpeg, "-loglevel", "error", "-y", *source, "-ac", "1", "-ar", rate, "{out}"])
        )
    if arecord := which("arecord"):
        found.append(ProcessRecorder("arecord", [arecord, "-q", "-f", "S16_LE", "-c", "1", "-r", rate, "{out}"]))
    choice = os.environ.get("HIGHHX_VOICE_RECORDER")
    if choice:
        found.sort(key=lambda r: r.name != choice)
    return found


# ------------------------------------------------------------------ analysis
@dataclass(frozen=True)
class AudioLevel:
    seconds: float
    peak: int

    @property
    def digital_silence(self) -> bool:
        """Every sample is exactly zero: the OS gave the recorder no microphone (permission)."""
        return self.seconds > 0 and self.peak == 0

    @property
    def quiet(self) -> bool:
        return self.peak < QUIET_PEAK


def analyse(path: Path) -> AudioLevel:
    """Duration and peak amplitude of a 16-bit WAV file."""
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate() or SAMPLE_RATE
            frames = audio.getnframes()
            width = audio.getsampwidth()
            data = audio.readframes(frames)
    except (OSError, wave.Error, EOFError):
        return AudioLevel(0.0, 0)
    if width != 2 or not data:
        return AudioLevel(frames / rate if rate else 0.0, 0 if not data else 32767)
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    peak = max((abs(s) for s in samples), default=0)
    return AudioLevel(frames / rate, peak)


def check_audio(path: Path) -> AudioLevel:
    """Raise the microphone problem a recording shows, else return its level."""
    level = analyse(path)
    if level.digital_silence:
        raise VoiceError("microphone-denied", "Microphone access is unavailable.", hint=MICROPHONE_HELP.get(_system()))
    return level


def record_for(recorder: ProcessRecorder, seconds: float) -> AudioLevel:
    """Record ``seconds`` of audio and analyse it (the setup microphone check)."""
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="highhx-mic-") as folder:
        path = Path(folder) / "check.wav"
        timer = threading.Timer(seconds, stop.set)
        timer.start()
        try:
            recorder.record(path, stop)
        finally:
            timer.cancel()
        return check_audio(path)
