"""Fake microphone, whisper.cpp, package manager, model server and speech for voice tests.

Nothing here touches real audio hardware, installs software or uses the network.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from highhx.voice.config import VoiceConfig
from highhx.voice.engines import Transcript, VoiceEngines, VoiceError
from highhx.voice.installer import Installer
from highhx.voice.model_manager import MODELS, ModelInfo, ModelManager
from highhx.voice.platform import VoicePlatform
from highhx.voice.recorder import AudioLevel
from highhx.voice.voice_manager import VoiceManager

MODEL_BYTES = b"lmgg" + b"\x01" * 2048
TEST_MODEL = ModelInfo("test.en", len(MODEL_BYTES), hashlib.sha256(MODEL_BYTES).hexdigest(), "test model")
MACOS = VoicePlatform("macos", "arm64", "brew")


class FakeRecorder:
    name = "fake-mic"

    def __init__(self, fail: VoiceError | None = None) -> None:
        self.fail = fail
        self.recordings = 0

    def record(self, path: Path, stop: Any) -> None:
        self.recordings += 1
        stop.wait(5)
        if self.fail is not None:
            raise self.fail
        path.write_bytes(b"RIFF" + b"\0" * 100)


class FakeTranscriber:
    """whisper.cpp stand-in: plain strings are heard confidently (0.95), like clear speech."""

    name = "whisper.cpp"

    def __init__(self, *heard: str | Transcript | Exception) -> None:
        self.heard = list(heard)
        self.calls = 0

    def transcribe(self, path: Path) -> Transcript:
        assert path.exists()
        self.calls += 1
        item = self.heard.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Transcript) else Transcript(item, 0.95)


class FakeSpeaker:
    name = "fake-tts"

    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def stop(self) -> None:
        return None


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class Machine:
    """A fake machine: which programs exist, what the package manager ran, what was downloaded."""

    def __init__(self, root: Path, *, recorder: bool = True, whisper: bool = True, model: bool = True) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.programs: dict[str, str] = {}
        self.ran: list[list[str]] = []
        self.downloads: list[str] = []
        self.download_data = MODEL_BYTES
        self.install_fails = False
        self.microphone: AudioLevel | VoiceError = AudioLevel(1.0, 1200)
        self.microphone_checks = 0
        self.recorder = FakeRecorder()
        self.speaker = FakeSpeaker()
        if recorder:
            self.programs["rec"] = "rec"
        if whisper:
            self.add_whisper()
        self.models_dir = root / "models"
        if model:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            (self.models_dir / TEST_MODEL.filename).write_bytes(MODEL_BYTES)

    def add_whisper(self) -> str:
        path = self.bin / "whisper-cli"
        path.write_text("#!/bin/sh\necho usage: --model\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.programs["whisper-cli"] = str(path)
        return str(path)

    def which(self, name: str) -> str | None:
        return self.programs.get(name)

    def runner(self, argv: list[str], log: Callable[[str], None]) -> int:
        self.ran.append(argv)
        log(f"==> {' '.join(argv)}")
        if self.install_fails:
            return 1
        if argv[-1] == "whisper-cpp":
            self.add_whisper()
        if argv[-1] in ("sox", "alsa-utils"):
            self.programs["rec"] = "rec"
        return 0

    def opener(self, url: str) -> FakeResponse:
        self.downloads.append(url)
        return FakeResponse(self.download_data)

    def microphone_check(self, _recorder: Any, _seconds: float) -> AudioLevel:
        self.microphone_checks += 1
        if isinstance(self.microphone, VoiceError):
            raise self.microphone
        return self.microphone

    def manager(self, platform: VoicePlatform = MACOS, config: VoiceConfig | None = None) -> VoiceManager:
        config = config or VoiceConfig.load()
        config.model = TEST_MODEL.name
        return VoiceManager(
            config,
            platform=platform,
            which=self.which,
            installer=Installer(platform, which=self.which, runner=self.runner),
            models=ModelManager(config, directory=self.models_dir, opener=self.opener),
            recorders=lambda: [self.recorder] if "rec" in self.programs else [],
            speaker=lambda: self.speaker,
            microphone_check=self.microphone_check,
            binary_check=lambda _binary: True,
        )


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    monkeypatch.setitem(MODELS, TEST_MODEL.name, TEST_MODEL)
    monkeypatch.setattr("highhx.voice.whisper.KNOWN_LOCATIONS", ())
    monkeypatch.setattr("highhx.voice.installer.KNOWN_LOCATIONS", ())
    monkeypatch.delenv("HIGHHX_WHISPER_MODEL", raising=False)
    return Machine(tmp_path / "machine")


def ready_engines(*heard: str | Transcript | Exception, speaker: FakeSpeaker | None = None) -> VoiceEngines:
    return VoiceEngines(
        recorder=FakeRecorder(), transcriber=FakeTranscriber(*heard), speaker=speaker or FakeSpeaker(), model="base.en"
    )


def fake_whisper_binary(folder: Path, text: str, tokens: list[tuple[str, float]], *, exit_code: int = 0) -> str:
    """A stand-in whisper-cli that writes whisper.cpp's --output-json-full file."""
    import json

    payload = {"transcription": [{"text": text, "tokens": [{"text": t, "p": p} for t, p in tokens]}]}
    script = folder / "whisper-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"if {exit_code}:\n"
        "    sys.stderr.write('error: failed to read audio\\n'); sys.exit(" + str(exit_code) + ")\n"
        "out = args[args.index('-of') + 1]\n"
        f"open(out + '.json', 'w').write(json.dumps({json.dumps(payload)}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert os.access(script, os.X_OK)
    return str(script)
