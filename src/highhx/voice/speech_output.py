"""Spoken replies with the operating system's own local speech (no cloud, no account)."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv; the text is a single argument, no shell
import sys
from dataclasses import dataclass, field

from highhx.voice.platform import Which

MAX_SPOKEN_CHARS = 400


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


def find_speakers(which: Which = shutil.which) -> list[ProcessSpeaker]:
    found: list[ProcessSpeaker] = []
    if sys.platform == "darwin" and which("say"):
        found.append(ProcessSpeaker("say", ["say", "-r", "205", "{text}"]))
    for binary in ("espeak-ng", "espeak"):
        if which(binary):
            found.append(ProcessSpeaker("espeak", [binary, "{text}"]))
            break
    if which("spd-say"):
        found.append(ProcessSpeaker("spd-say", ["spd-say", "-w", "{text}"]))
    if sys.platform == "win32" and which("powershell"):
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($args[0])"
        )
        found.append(ProcessSpeaker("sapi", ["powershell", "-NoProfile", "-Command", script, "{text}"]))
    return found


def choose_speaker(which: Which = shutil.which) -> ProcessSpeaker | None:
    choice = os.environ.get("HIGHHX_VOICE_TTS")
    if choice == "off":
        return None
    speakers = find_speakers(which)
    if choice:
        return next((s for s in speakers if s.name == choice), None)
    return speakers[0] if speakers else None
