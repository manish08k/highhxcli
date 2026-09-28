"""Voice engines: the local programs that record, transcribe and speak.

Nothing here talks to a network service or a language model. Recording and speech use the
operating system's tools; speech-to-text is whisper.cpp with a model HighhX manages. Voice
only turns speech into the text a person would have typed — every request then takes the
session's normal path (resolver or agent → action plan → risk → approval → executor →
verification → audit).

Advanced overrides (never needed for normal use):

    HIGHHX_VOICE_RECORDER   rec | ffmpeg | arecord
    HIGHHX_VOICE_TTS        say | espeak | spd-say | sapi | off
    HIGHHX_WHISPER_MODEL    path to a whisper.cpp model file to use instead of the managed one
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from highhx.core.errors import IntegrationError

SAMPLE_RATE = 16000


class VoiceError(IntegrationError):
    """A voice problem with a recovery path: ``problem`` names it, ``hint`` says what to do."""

    category = "voice"

    def __init__(self, problem: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.problem = problem


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None = None
    """whisper.cpp's mean token probability (0 to 1), or None when the engine does not report one."""


class Recorder(Protocol):
    name: str

    def record(self, path: Path, stop: object) -> None:
        """Record 16 kHz mono WAV into ``path`` until ``stop`` (a ``threading.Event``) is set."""
        ...


class Transcriber(Protocol):
    name: str

    def transcribe(self, path: Path) -> Transcript | str: ...


class Speaker(Protocol):
    name: str

    def say(self, text: str) -> None: ...

    def stop(self) -> None: ...


def as_transcript(value: Transcript | str) -> Transcript:
    return value if isinstance(value, Transcript) else Transcript(str(value))


@dataclass
class VoiceEngines:
    recorder: Recorder | None = None
    transcriber: Transcriber | None = None
    speaker: Speaker | None = None
    notes: list[str] = field(default_factory=list)
    model: str | None = None

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


def detect() -> VoiceEngines:
    """The engines ready on this machine now — detection only: nothing is installed or downloaded."""
    from highhx.voice.voice_manager import VoiceManager

    return VoiceManager().engines()
