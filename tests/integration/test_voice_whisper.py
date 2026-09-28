"""Prerecorded speech → real whisper.cpp → the real session pipeline.

Runs where whisper.cpp and a model are installed (``/voice setup`` on a developer machine);
skipped otherwise, e.g. in CI. Set HIGHHX_TEST_WHISPER_MODEL to use a specific model file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.repl import AgentREPL
from highhx.voice.engines import VoiceEngines
from highhx.voice.mode import VoiceMode
from highhx.voice.whisper import KNOWN_LOCATIONS, WhisperCppTranscriber
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401
from tests.unit.agent.test_repl import Script, make_ui
from tests.unit.automation.fakes import browser, media  # noqa: F401
from tests.unit.voice.fakes import FakeSpeaker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "voice"


def _model() -> Path | None:
    configured = os.environ.get("HIGHHX_TEST_WHISPER_MODEL")
    candidates = [Path(configured)] if configured else []
    home = Path.home()
    candidates += [
        home / "Library" / "Application Support" / "highhx" / "voice" / "models" / "ggml-base.en.bin",
        home / ".local" / "share" / "highhx" / "voice" / "models" / "ggml-base.en.bin",
    ]
    return next((c for c in candidates if c.is_file()), None)


def _binary() -> str | None:
    found = shutil.which("whisper-cli") or next((p for p in KNOWN_LOCATIONS if Path(p).is_file()), None)
    return found


MODEL, BINARY = _model(), _binary()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MODEL is None or BINARY is None, reason="whisper.cpp and a model are not installed"),
]


class FileRecorder:
    """Plays a prerecorded WAV file as the microphone."""

    name = "fixture"

    def __init__(self, source: Path) -> None:
        self.source = source

    def record(self, path: Path, stop: Any) -> None:
        stop.wait(5)
        shutil.copyfile(self.source, path)


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


def spoken_session(app: Any, fixture: str) -> tuple[AgentREPL, Any, FakeSpeaker]:
    assert MODEL is not None and BINARY is not None
    script = Script("", "", "/quit")  # Enter starts, Enter stops the "recording"
    ui, buffer = make_ui(script)
    speaker = FakeSpeaker()
    engines = VoiceEngines(
        recorder=FileRecorder(FIXTURES / fixture),
        transcriber=WhisperCppTranscriber(BINARY, MODEL),
        speaker=speaker,
        model="base.en",
    )
    mode = VoiceMode(ui, app.ctx.events, engines=engines)
    return AgentREPL(None, ui, None, app=app, read_line=script, voice=True, voice_mode=mode), buffer, speaker


def test_real_whisper_transcribes_the_fixture() -> None:
    assert MODEL is not None and BINARY is not None
    transcript = WhisperCppTranscriber(BINARY, MODEL).transcribe(FIXTURES / "show_git_status.wav")
    assert transcript.text.lower() == "show git status"
    assert transcript.confidence is not None and 0 < transcript.confidence <= 1


def test_spoken_git_status_runs_through_the_session(agent_project: Path, make_app: Any) -> None:  # noqa: F811
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=agent_project, check=True, capture_output=True)
    repl, buffer, speaker = spoken_session(make_app(agent_project), "show_git_status.wav")
    repl.run()
    out = buffer.getvalue()
    assert "◉ Transcribing..." in out and "◉ git status  git.status · safe" in out and "✓ git status" in out
    assert speaker.said  # a spoken request gets a spoken reply


def test_spoken_youtube_request_runs_the_browser_plan(agent_project: Path, make_app: Any, media: Any) -> None:  # noqa: F811
    repl, buffer, _ = spoken_session(make_app(agent_project), "open_youtube_and_play.wav")
    repl.run()
    out = buffer.getvalue()
    assert "Open YouTube and play" in out
    assert media.visited[-1] == "https://www.youtube.com/watch?v=abc123"  # open → search → play, verified
    assert "3/3 steps" in out
