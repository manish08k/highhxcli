"""whisper.cpp: the local speech-to-text engine.

HighhX runs whisper.cpp's CLI (``whisper-cli``) on a recorded WAV file with the managed
model and reads its full JSON output, which carries a probability for every token; their
mean is the transcript's confidence. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 - fixed argv to the local whisper.cpp binary; no shell
import tempfile
from dataclasses import dataclass
from pathlib import Path

from highhx.voice.config import VoiceConfig, voice_data_dir
from highhx.voice.engines import Transcript, VoiceError
from highhx.voice.platform import Which

BINARY_NAMES = ("whisper-cli", "whisper-cpp")
KNOWN_LOCATIONS: tuple[str, ...] = ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli")
"""Homebrew's install locations (checked even when PATH does not include them)."""
TIMEOUT = 120
NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪")
"""What whisper.cpp writes for non-speech: [BLANK_AUDIO], (music), [ Silence ], ♪ …"""


def managed_binary() -> Path:
    """Where HighhX builds whisper.cpp when no package manager provides it."""
    return voice_data_dir() / "whisper.cpp" / "build" / "bin" / "whisper-cli"


def find_binary(config: VoiceConfig, which: Which = shutil.which) -> str | None:
    """The whisper.cpp CLI: the one setup recorded, one on PATH, Homebrew's, or HighhX's own build."""
    candidates: list[str | None] = [config.whisper_binary]
    candidates += [which(name) for name in BINARY_NAMES]
    candidates += [*KNOWN_LOCATIONS, str(managed_binary())]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and _executable(Path(candidate)):
            return candidate
    return None


def _executable(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)


def verify_binary(binary: str) -> bool:
    """The binary runs and is whisper.cpp (its help mentions the model option)."""
    try:
        done = subprocess.run(  # nosec B603 - fixed argv
            [binary, "--help"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--model" in (done.stdout + done.stderr)


def clean(text: str) -> str:
    text = NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def confidence(data: dict[str, object]) -> float | None:
    """Mean probability of the spoken tokens in whisper.cpp's ``--output-json-full``."""
    probabilities: list[float] = []
    for segment in data.get("transcription") or []:  # type: ignore[attr-defined]
        for token in segment.get("tokens") or []:
            text = str(token.get("text", ""))
            if text.startswith("[_") or not text.strip() or "p" not in token:
                continue
            probabilities.append(float(token["p"]))
    return sum(probabilities) / len(probabilities) if probabilities else None


@dataclass
class WhisperCppTranscriber:
    binary: str
    model: Path
    language: str = "en"
    name: str = "whisper.cpp"

    def transcribe(self, path: Path) -> Transcript:
        with tempfile.TemporaryDirectory(prefix="highhx-whisper-") as folder:
            base = Path(folder) / "out"
            argv = [
                self.binary,
                "-m",
                str(self.model),
                "-f",
                str(path),
                "-l",
                self.language,
                "-nt",
                "-np",
                "-ojf",
                "-of",
                str(base),
            ]
            try:
                done = subprocess.run(  # nosec B603 - fixed argv
                    argv, capture_output=True, text=True, timeout=TIMEOUT, check=False
                )
            except FileNotFoundError:
                raise VoiceError(
                    "whisper-unavailable", "whisper.cpp is no longer installed.", hint="Run /voice setup."
                ) from None
            except subprocess.TimeoutExpired:
                raise VoiceError(
                    "transcription-failed", "Transcription took too long.", hint="Try a shorter request."
                ) from None
            if done.returncode != 0:
                raise VoiceError(
                    "transcription-failed",
                    "Transcription failed.",
                    hint=(done.stderr.strip().splitlines() or ["Run /voice test to check the setup."])[-1][:300],
                )
            output = base.with_suffix(".json")
            if output.is_file():
                try:
                    data = json.loads(output.read_text(encoding="utf-8", errors="replace"))
                except ValueError:
                    data = {}
                text = " ".join(str(s.get("text", "")) for s in data.get("transcription") or [])
                return Transcript(clean(text), confidence(data))
            return Transcript(clean(done.stdout), None)
