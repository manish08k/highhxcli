"""HighhX Voice: whisper.cpp setup, model management, microphone handling, and voice as an
input to the *same* session pipeline — with no special path around resolver, risk, approval,
executor or verification. Everything runs against fakes: no microphone, no whisper.cpp, no
network, no package manager."""

from __future__ import annotations

import array
import json
import wave
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.repl import AgentREPL
from highhx.voice.config import VoiceConfig
from highhx.voice.engines import Transcript, VoiceEngines, VoiceError
from highhx.voice.mode import VoiceMode
from highhx.voice.platform import VoicePlatform
from highhx.voice.recorder import AudioLevel, analyse, check_audio, find_recorders
from highhx.voice.whisper import WhisperCppTranscriber, clean, confidence
from tests.unit.agent.test_repl import Script, make_ui
from tests.unit.voice.fakes import (
    MACOS,
    MODEL_BYTES,
    TEST_MODEL,
    FakeRecorder,
    FakeSpeaker,
    FakeTranscriber,
    Machine,
    fake_whisper_binary,
    ready_engines,
)


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


class Recording:
    """A SetupUI that answers from a script and keeps what was printed."""

    def __init__(self, *answers: bool, interactive: bool = True) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []
        self.out: list[str] = []
        self.interactive = interactive

    def print(self, renderable: Any = "") -> None:
        self.out.append(str(renderable))

    def ask(self, question: str, *, default: bool = False) -> bool:
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else default

    @property
    def text(self) -> str:
        return "\n".join(self.out)


def session(app: Any, *lines: str, engines: VoiceEngines | None = None, manager: Any = None, voice: bool = False):
    script = Script(*lines)
    ui, buffer = make_ui(script)
    mode = VoiceMode(ui, app.ctx.events, engines=engines, manager=manager)
    repl = AgentREPL(None, ui, None, app=app, read_line=script, voice=voice, voice_mode=mode)
    return repl, buffer


def wav(path: Path, samples: list[int], rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(array.array("h", samples).tobytes())
    return path


# ======================================================================= status
def test_status_ready(machine: Machine) -> None:
    status = machine.manager().status()
    assert status.ready and status.missing == []
    assert status.to_dict()["stt"] == "whisper.cpp" and status.recorder == "fake-mic"


def test_status_names_missing_whisper_with_a_fix(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    status = machine.manager().status()
    assert not status.ready
    [missing] = status.missing
    assert missing.name == "whisper.cpp" and missing.detail == "not installed" and missing.fix == "/voice setup"


def test_status_names_missing_model(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()
    status = machine.manager().status()
    assert [c.name for c in status.missing] == ["Model"] and status.model_state == "missing"


def test_status_names_missing_recorder(machine: Machine) -> None:
    machine.programs.pop("rec")
    assert [c.name for c in machine.manager().status().missing] == ["Recorder"]


def test_status_reports_an_unsupported_platform(machine: Machine) -> None:
    windows = VoicePlatform("windows", "AMD64", None)
    status = machine.manager(platform=windows).status()
    assert not status.ready and "not supported on Windows" in status.missing[0].detail


def test_status_detects_without_installing_or_recording(machine: Machine) -> None:
    machine.programs.pop("rec")
    machine.manager().status()
    assert machine.ran == [] and machine.downloads == [] and machine.microphone_checks == 0


# ======================================================================== setup
def test_setup_installs_missing_whisper_after_consent_and_remembers_it(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    ui = Recording(True)
    status = machine.manager().setup(ui)
    assert "🎙 HighhX Voice setup" in ui.text and "Whisper.cpp is not installed." in ui.text
    assert ui.questions == ["Set up local voice now?"]
    assert machine.ran == [["brew", "install", "whisper-cpp"]]
    assert status.ready
    assert VoiceConfig.load().whisper_binary == str(machine.bin / "whisper-cli")  # persisted


def test_setup_installs_the_recorder(machine: Machine) -> None:
    machine.programs.pop("rec")
    status = machine.manager().setup(Recording(True))
    assert machine.ran == [["brew", "install", "sox"]] and status.ready


def test_declining_setup_installs_nothing(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    ui = Recording(False)
    status = machine.manager().setup(ui)
    assert machine.ran == [] and not status.ready and "/voice on" in ui.text


def test_setup_never_installs_without_a_person(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    (machine.models_dir / TEST_MODEL.filename).unlink()
    machine.manager().setup(Recording(interactive=False))
    assert machine.ran == [] and machine.downloads == []


def test_setup_does_not_reinstall_or_redownload(machine: Machine) -> None:
    ui = Recording()
    machine.manager().setup(ui)
    machine.manager().setup(ui)
    assert machine.ran == [] and machine.downloads == [] and ui.questions == []
    assert machine.microphone_checks == 1  # checked once, then remembered


def test_setup_install_failure_has_a_recovery_path(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    machine.install_fails = True
    ui = Recording(True)
    status = machine.manager().setup(ui)
    assert not status.ready and "Installing whisper.cpp with Homebrew failed." in ui.text
    assert "Traceback" not in ui.text


def test_setup_on_a_mac_without_homebrew_explains_how_to_recover(machine: Machine) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    ui = Recording(True)
    machine.manager(platform=VoicePlatform("macos", "arm64", None)).setup(ui)
    assert "cannot install whisper.cpp" in ui.text and "brew.sh" in ui.text and machine.ran == []


def test_setup_on_an_unsupported_platform(machine: Machine) -> None:
    ui = Recording(True)
    machine.manager(platform=VoicePlatform("windows", "AMD64", None)).setup(ui)
    assert "not supported on Windows" in ui.text and machine.ran == [] and ui.questions == []


def test_linux_builds_whisper_from_source(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.voice.installer import WHISPER_TAG, Installer
    from highhx.voice.whisper import managed_binary

    for tool in ("git", "cmake", "c++"):
        machine.programs[tool] = tool

    def runner(argv: list[str], log: Any) -> int:
        machine.ran.append(argv)
        if argv[0] == "git":
            Path(argv[-1]).mkdir(parents=True)
            (Path(argv[-1]) / "CMakeLists.txt").write_text("")
        if "--build" in argv:
            managed_binary().parent.mkdir(parents=True)
            managed_binary().write_text("")
        return 0

    linux = VoicePlatform("linux", "x86_64", "apt-get")
    installer = Installer(linux, which=machine.which, runner=runner)
    assert "from source" in (installer.whisper_plan() or "")
    assert installer.install_whisper(lambda _line: None) == str(managed_binary())
    assert machine.ran[0][:5] == ["git", "clone", "--depth", "1", "--branch"] and WHISPER_TAG in machine.ran[0]
    assert [a[0] for a in machine.ran] == ["git", "cmake", "cmake"]


def test_linux_recorder_uses_the_system_package_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.voice.installer import Installer

    monkeypatch.setattr("os.geteuid", lambda: 1000)
    ran: list[list[str]] = []
    installer = Installer(VoicePlatform("linux", "x86_64", "apt-get"), runner=lambda a, _l: ran.append(a) or 0)
    installer.install_recorder(lambda _line: None)
    assert ran == [["sudo", "apt-get", "install", "-y", "alsa-utils"]]


# ======================================================================== model
def test_missing_model_is_downloaded_with_progress_and_verified(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()
    ui = Recording(True)
    status = machine.manager().setup(ui)
    assert "Whisper model not found." in ui.text
    assert ui.questions == [f"Download required voice model (test.en, {TEST_MODEL.size_label})?"]
    assert machine.downloads == [TEST_MODEL.url]
    assert "Downloading test.en: 100%" in ui.text and "verified (SHA-256)" in ui.text
    assert (machine.models_dir / TEST_MODEL.filename).read_bytes() == MODEL_BYTES
    assert status.ready and VoiceConfig.load().verified_models["test.en"]["sha256"] == TEST_MODEL.sha256


def test_the_model_cache_is_reused(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()
    machine.manager().setup(Recording(True))
    ui = Recording()
    assert machine.manager().setup(ui).ready
    assert machine.downloads == [TEST_MODEL.url] and ui.questions == []


def test_models_live_outside_the_project(machine: Machine, tmp_path: Path) -> None:
    from highhx.utils.paths import user_data_dir
    from highhx.voice.config import models_dir

    assert models_dir().is_relative_to(user_data_dir()) and not models_dir().is_relative_to(Path.cwd())


def test_a_corrupted_model_is_detected_and_replaced(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).write_bytes(b"lmgg" + b"\x00" * 10)
    manager = machine.manager()
    assert manager.status().model_state == "corrupted"
    ui = Recording(True)
    assert manager.setup(ui).ready and "Whisper model is damaged." in ui.text


def test_a_model_with_the_wrong_checksum_is_not_used(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).write_bytes(b"lmgg" + b"\x02" * 2048)  # right size, wrong bytes
    assert machine.manager().status().model_state == "corrupted"


def test_a_download_that_fails_its_checksum_installs_nothing(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()
    machine.download_data = b"lmgg" + b"\x09" * 2048
    ui = Recording(True)
    status = machine.manager().setup(ui)
    assert not status.ready and "did not match its checksum" in ui.text
    assert list(machine.models_dir.iterdir()) == []  # no .part left behind


def test_a_failed_download_explains_the_recovery(machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()

    def offline(_url: str) -> Any:
        raise OSError("network is unreachable")

    manager = machine.manager()
    manager.models.opener = offline
    ui = Recording(True)
    assert not manager.setup(ui).ready
    assert "Downloading the test.en voice model failed." in ui.text and "/voice setup" in ui.text


def test_downloads_verify_tls_even_without_a_python_ca_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import ssl

    from highhx.voice import model_manager

    loaded: list[str] = []
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(ssl, "create_default_context", lambda: empty)
    monkeypatch.setattr(empty, "load_verify_locations", loaded.append, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "certifi", None)  # not installed
    bundle = tmp_path / "cert.pem"
    bundle.write_text("")
    monkeypatch.setattr(model_manager, "SYSTEM_CA_BUNDLES", (str(tmp_path / "missing.pem"), str(bundle)))
    context = model_manager.ssl_context()
    assert loaded == [str(bundle)]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_the_default_model_is_base_en_with_a_published_checksum() -> None:
    from highhx.voice.model_manager import MODELS

    base = MODELS["base.en"]
    assert VoiceConfig().model == "base.en"
    assert base.size == 147_964_211 and len(base.sha256) == 64
    assert base.url == "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"


# =================================================================== microphone
def test_microphone_denied_during_setup_offers_a_retry(machine: Machine) -> None:
    from highhx.voice.recorder import MICROPHONE_HELP

    machine.microphone = VoiceError(
        "microphone-denied", "Microphone access is unavailable.", hint=MICROPHONE_HELP["macos"]
    )
    manager = machine.manager()
    ui = Recording(False)
    manager.setup(ui)
    assert "Microphone access is unavailable." in ui.text
    assert "System Settings → Privacy & Security → Microphone" in ui.text
    assert ui.questions == ["Retry the microphone?"]
    assert manager.status().microphone == "denied" and not manager.status().ready


def test_microphone_retry_succeeds(machine: Machine) -> None:
    manager = machine.manager()
    machine.microphone = VoiceError("microphone-denied", "Microphone access is unavailable.")
    answers = iter([True])

    class Retry(Recording):
        def ask(self, question: str, *, default: bool = False) -> bool:
            machine.microphone = AudioLevel(1.0, 900)  # permission granted meanwhile
            return next(answers)

    assert manager.setup(Retry()).ready and manager.config.microphone == "ready"


def test_digital_silence_means_microphone_permission(tmp_path: Path) -> None:
    silent = wav(tmp_path / "silent.wav", [0] * 16000)
    with pytest.raises(VoiceError) as caught:
        check_audio(silent)
    assert caught.value.problem == "microphone-denied"
    speech = wav(tmp_path / "speech.wav", [0, 1200, -900, 30] * 4000)
    level = check_audio(speech)
    assert level.seconds == pytest.approx(1.0) and level.peak == 1200 and not level.quiet
    assert analyse(tmp_path / "missing.wav") == AudioLevel(0.0, 0)


def test_recorder_detection() -> None:
    programs = {"rec": "/usr/bin/rec", "ffmpeg": "/usr/bin/ffmpeg", "arecord": "/usr/bin/arecord"}
    mac = find_recorders(MACOS, programs.get)
    assert [r.name for r in mac] == ["rec", "ffmpeg", "arecord"]
    assert "avfoundation" in mac[1].argv
    linux = find_recorders(VoicePlatform("linux", "x86_64", "apt-get"), programs.get)
    assert "alsa" in linux[1].argv
    assert find_recorders(MACOS, {}.get) == []


def test_a_recorder_override_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_VOICE_RECORDER", "ffmpeg")
    programs = {"rec": "/usr/bin/rec", "ffmpeg": "/usr/bin/ffmpeg"}
    assert find_recorders(MACOS, programs.get)[0].name == "ffmpeg"


# ================================================================ transcription
def test_whisper_transcription_with_confidence(tmp_path: Path) -> None:
    binary = fake_whisper_binary(
        tmp_path,
        " Open YouTube and play Adhento Gani.",
        [("[_BEG_]", 0.1), (" Open", 0.98), (" YouTube", 0.96), (" and", 0.99), (" play", 0.97), (" Adhento", 0.7)],
    )
    audio = wav(tmp_path / "a.wav", [500] * 1600)
    transcript = WhisperCppTranscriber(binary, tmp_path / "model.bin").transcribe(audio)
    assert transcript.text == "Open YouTube and play Adhento Gani"
    assert transcript.confidence == pytest.approx((0.98 + 0.96 + 0.99 + 0.97 + 0.7) / 5)


def test_whisper_failure_is_a_voice_error(tmp_path: Path) -> None:
    binary = fake_whisper_binary(tmp_path, "", [], exit_code=3)
    with pytest.raises(VoiceError) as caught:
        WhisperCppTranscriber(binary, tmp_path / "m.bin").transcribe(wav(tmp_path / "a.wav", [1] * 10))
    assert caught.value.problem == "transcription-failed" and "failed to read audio" in (caught.value.hint or "")


def test_a_missing_whisper_binary_is_a_voice_error(tmp_path: Path) -> None:
    with pytest.raises(VoiceError) as caught:
        WhisperCppTranscriber(str(tmp_path / "gone"), tmp_path / "m.bin").transcribe(tmp_path / "a.wav")
    assert caught.value.problem == "whisper-unavailable"


def test_non_speech_markers_are_removed() -> None:
    assert clean(" [BLANK_AUDIO] ") == ""
    assert clean("(music) run the tests. ♪") == "run the tests"
    assert confidence({"transcription": [{"tokens": [{"text": "[_TT_1]", "p": 0.1}]}]}) is None


# ====================================================================== config
def test_config_round_trip_and_damage(tmp_path: Path) -> None:
    path = tmp_path / "voice.json"
    VoiceConfig(model="small.en", replies=False, microphone="ready").save(path)
    loaded = VoiceConfig.load(path)
    assert (loaded.model, loaded.replies, loaded.microphone) == ("small.en", False, "ready")
    path.write_text("{not json")
    assert VoiceConfig.load(path) == VoiceConfig()
    path.write_text(json.dumps({"confirm": "never", "mode": "always-on", "unknown": 1}))
    loaded = VoiceConfig.load(path)
    assert loaded.confirm == "auto" and loaded.mode == "push-to-talk"  # nothing listens continuously


# ============================================================= session: /voice
def test_voice_on_shows_the_setup_and_starts_listening(agent_project: Path, make_app, machine: Machine) -> None:
    app = make_app(agent_project)
    machine.manager().setup(Recording())  # already set up
    repl, buffer = session(app, "/voice on", "", "", "/quit", manager=machine.manager())
    repl._voice._engines = None  # type: ignore[union-attr]
    engines = machine.manager().engines()
    engines.transcriber = FakeTranscriber("show git status")
    repl._voice._engines = engines  # type: ignore[union-attr]
    repl.run()
    out = buffer.getvalue()
    assert "🎙 Voice on" in out and "Speech-to-text: whisper.cpp" in out
    assert "Microphone: ready" in out and "Voice replies: enabled" in out
    assert "🎙 Listening..." in out and "Speak now." in out and "◉ Transcribing..." in out
    assert "✓ “show git status”" in out and "◉ git status" in out
    assert machine.speaker.said == ["git status: done."] or machine.speaker.said


def test_voice_on_runs_first_time_setup(agent_project: Path, make_app, machine: Machine, tmp_path: Path) -> None:
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    (machine.models_dir / TEST_MODEL.filename).unlink()
    app = make_app(agent_project)
    # /voice on → set up? y → download model? y → listening → Enter → transcript → /quit
    repl, buffer = session(app, "/voice on", "y", "y", "", "", "/quit", manager=machine.manager())

    real_engines = repl.voice.manager.engines

    def engines() -> VoiceEngines:
        found = real_engines()
        if found.transcriber is not None:
            found.transcriber = FakeTranscriber("show git status")  # stands in for whisper.cpp
        return found

    repl.voice.manager.engines = engines  # type: ignore[method-assign]
    repl.run()
    out = buffer.getvalue()
    prompts = " ".join(repl._read_line.prompts)  # type: ignore[attr-defined]
    assert "Whisper.cpp is not installed." in out and "Set up local voice now?" in prompts
    assert "Whisper model not found." in out and "Download required voice model" in prompts
    assert "verified (SHA-256)" in out and "Checking the microphone" in out
    assert machine.ran == [["brew", "install", "whisper-cpp"]] and machine.downloads == [TEST_MODEL.url]
    assert "🎙 Voice on" in out and "◉ git status" in out


def test_voice_status_in_the_session(agent_project: Path, make_app, machine: Machine) -> None:
    (machine.models_dir / TEST_MODEL.filename).unlink()
    app = make_app(agent_project)
    repl, buffer = session(app, "/voice status", "/quit", manager=machine.manager())
    repl.run()
    out = buffer.getvalue()
    assert "🎙 HighhX Voice" in out
    for row in (
        "Status",
        "Speech-to-text",
        "Model",
        "Microphone",
        "Recorder",
        "Voice replies",
        "Push-to-talk",
        "Setup",
    ):
        assert row in out
    assert "OFF" in out and "test.en (missing)" in out and "unavailable" in out and "incomplete" in out
    assert "Model: test.en (missing) → /voice setup" in out
    assert machine.ran == [] and machine.downloads == [] and machine.microphone_checks == 0


def test_voice_off_stops_listening(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    engines = ready_engines("show git status")
    repl, buffer = session(app, "", "", "/voice off", "", "/quit", engines=engines, voice=True)
    repl.run()
    out = buffer.getvalue()
    assert "🎙 Voice off." in out
    assert out.count("🎙 Listening...") == 1  # the empty line after /voice off does not record
    assert engines.recorder.recordings == 1  # type: ignore[union-attr]


def test_voice_test_transcribes_and_runs_nothing(agent_project: Path, make_app, browser: dict[str, Any]) -> None:
    app = make_app(agent_project)
    engines = ready_engines(Transcript("open GitHub", 0.93))
    repl, buffer = session(app, "/voice test", "", "/quit", engines=engines)
    repl.run()
    out = buffer.getvalue()
    assert "✓ “open GitHub”" in out and "confidence 93%" in out and "nothing was run" in out
    assert browser["flows"] == [] and "◉ open" not in out


def test_voice_mute_and_unmute(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    speaker = FakeSpeaker()
    engines = ready_engines("run the tests", speaker=speaker)
    repl, buffer = session(app, "/voice mute", "/voice on", "", "", "/quit", engines=engines)
    repl.run()
    assert "Voice replies muted." in buffer.getvalue() and speaker.said == []


def test_help_documents_voice(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer = session(app, "/help", "/quit")
    repl.run()
    out = buffer.getvalue()
    for line in ("/voice on", "/voice off", "/voice status", "/voice setup", "/voice test"):
        assert line in out
    assert "/help · /voice · /status" in out  # the start-up hint


def test_highhx_voice_inside_the_session(agent_project: Path, make_app, machine: Machine) -> None:
    app = make_app(agent_project)
    repl, buffer = session(app, "highhx voice status", "highhx voice", "n", "/quit", manager=machine.manager())
    machine.programs.pop("whisper-cli")
    (machine.bin / "whisper-cli").unlink()
    repl.run()
    out = buffer.getvalue()
    assert "already in the HighhX session" not in out
    assert "In the session this is /voice status." in out and "🎙 HighhX Voice" in out
    assert "In the session this is /voice on." in out and "Whisper.cpp is not installed." in out


# ================================================ session: the same pipeline
def test_the_transcript_takes_the_typed_path(agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice produces text and hands it to AgentREPL.handle — nothing else."""
    handled: list[str] = []
    original = AgentREPL.handle

    def spy(self: AgentREPL, text: str) -> None:
        handled.append(text)
        original(self, text)

    monkeypatch.setattr(AgentREPL, "handle", spy)
    requests: list[str] = []
    monkeypatch.setattr(AgentREPL, "local_request", lambda self, text: requests.append(text))
    app = make_app(agent_project)
    repl, _ = session(
        app,
        "",  # Enter on an empty line: talk
        "",  # Enter: stop → transcribed
        "open YouTube and play Adhento Gani",  # the same request, typed
        "/quit",
        engines=ready_engines("open YouTube and play Adhento Gani"),
        voice=True,
    )
    repl.run()
    assert handled[:2] == ["open YouTube and play Adhento Gani", "open YouTube and play Adhento Gani"]
    assert requests == ["open YouTube and play Adhento Gani"] * 2  # spoken and typed: identical calls


def test_voice_browser_automation(agent_project: Path, make_app, media: Any) -> None:
    app = make_app(agent_project)
    speaker = FakeSpeaker()
    repl, buffer = session(
        app, "", "", "/quit", engines=ready_engines("Open YouTube and play Adhento Gani.", speaker=speaker), voice=True
    )
    repl.run()
    out = buffer.getvalue()
    assert "✓ “Open YouTube and play Adhento Gani”" in out
    assert media.visited[-1] == "https://www.youtube.com/watch?v=abc123"  # open → search → play, verified
    assert "↳ verified" in out and speaker.said


def test_voice_safe_command_runs_without_a_prompt(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer = session(app, "", "", "/quit", engines=ready_engines("show git status"), voice=True)
    repl.run()
    out = buffer.getvalue()
    assert "◉ git status  git.status · safe" in out and "Approv" not in out


def test_voice_cannot_bypass_approval(agent_project: Path, make_app) -> None:
    """A confident transcript of a risky request stops at the same approval as typing it."""
    app = make_app(agent_project)
    spoken, spoken_out = session(app, "", "", "n", "/quit", engines=ready_engines(Transcript("push", 0.99)), voice=True)
    spoken.run()
    typed, typed_out = session(app, "push", "n", "/quit")
    typed.run()
    out = spoken_out.getvalue()
    assert "✓ “push”" in out  # heard confidently: no voice confirmation …
    assert "◉ push to origin  git.push · high" in out and "Approval required" in out  # … the executor still asks
    assert "Cancelled." in out and "⊘ push to origin — not approved" in out
    approvals = [p for p in spoken._read_line.prompts if "Approve?" in p]  # type: ignore[attr-defined]
    assert approvals and approvals == [p for p in typed._read_line.prompts if "Approve?" in p]  # type: ignore[attr-defined]
    assert "⊘ push to origin — not approved" in typed_out.getvalue()


def test_microphone_unavailable_while_listening(agent_project: Path, make_app) -> None:
    from highhx.voice.recorder import MICROPHONE_HELP

    app = make_app(agent_project)
    engines = ready_engines("unused")
    engines.recorder = FakeRecorder(
        fail=VoiceError("microphone-unavailable", "Microphone access is unavailable.", hint=MICROPHONE_HELP["macos"])
    )
    repl, buffer = session(app, "", "", "show git status", "/quit", engines=engines, voice=True)
    repl.run()
    out = buffer.getvalue()
    assert "⚠ Microphone access is unavailable." in out
    assert "System Settings → Privacy & Security → Microphone" in out
    assert "◉ git status" in out and "Traceback" not in out and "Unexpected error" not in out


def test_transcription_failure_keeps_the_session(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    failure = VoiceError("transcription-failed", "Transcription failed.", hint="Run /voice test to check the setup.")
    repl, buffer = session(app, "", "", "show git status", "/quit", engines=ready_engines(failure), voice=True)
    repl.run()
    out = buffer.getvalue()
    assert "Transcription failed." in out and "◉ git status" in out and "Traceback" not in out


def test_nothing_heard(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer = session(app, "", "", "/quit", engines=ready_engines(Transcript("[BLANK_AUDIO]", 0.9)), voice=True)
    repl.run()
    assert "Nothing was heard." in buffer.getvalue() and "◉ " not in buffer.getvalue().replace("◉ Transcribing", "")


def test_ctrl_c_cancels_a_recording(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)

    class Interrupting(Script):
        def __call__(self, prompt: str) -> str:
            if prompt == "" and not getattr(self, "done", False):
                self.done = True
                raise KeyboardInterrupt
            return super().__call__(prompt)

    script = Interrupting("", "/quit")  # Enter on an empty line starts recording
    ui, buffer = make_ui(script)
    engines = ready_engines("run the tests")
    repl = AgentREPL(
        None, ui, None, app=app, read_line=script, voice=True, voice_mode=VoiceMode(ui, app.ctx.events, engines=engines)
    )
    repl.run()
    assert "Recording cancelled." in buffer.getvalue() and "◉ run" not in buffer.getvalue()


def test_confirm_always_setting(agent_project: Path, make_app, machine: Machine) -> None:
    config = VoiceConfig.load()
    config.confirm = "always"
    manager = machine.manager(config=config)
    app = make_app(agent_project)
    repl, buffer = session(
        app,
        "",
        "",
        "n",
        "/quit",
        engines=ready_engines(Transcript("show git status", 0.99)),
        manager=manager,
        voice=True,
    )
    repl.run()
    assert "Heard: “show git status” (confidence 99%)" in buffer.getvalue() and "Not run." in buffer.getvalue()


# ========================================================================= CLI
def test_cli_voice_status_json(cli: Any, tmp_path: Path) -> None:
    data = cli("voice", "status", "--json", cwd=tmp_path).json()
    assert data["stt"] == "whisper.cpp" and data["model"] == "base.en" and data["mode"] == "push-to-talk"
    assert isinstance(data["missing"], list)


def test_cli_voice_status_human(cli: Any, tmp_path: Path) -> None:
    result = cli("voice", "status", cwd=tmp_path)
    assert result.code == 0 and "HighhX Voice" in result.stdout and "whisper.cpp" in result.stdout


def test_cli_voice_model(cli: Any, tmp_path: Path) -> None:
    data = cli("voice", "model", "--json", cwd=tmp_path).json()
    assert data["model"] == "base.en" and {m["name"] for m in data["available"]} >= {"tiny.en", "base.en", "small.en"}
    assert cli("voice", "model", "small.en", "--json", cwd=tmp_path).json()["model"] == "small.en"
    assert VoiceConfig.load().model == "small.en"
    bad = cli("voice", "model", "huge.xx", cwd=tmp_path)
    assert bad.code != 0 and "Unknown voice model" in bad.stdout + bad.stderr


def test_cli_voice_setup_asks_first(cli: Any, tmp_path: Path) -> None:
    result = cli("voice", "setup", cwd=tmp_path)
    assert result.code != 0 and "asks first" in result.stdout + result.stderr


def test_cli_voice_test_needs_setup_or_a_terminal(cli: Any, tmp_path: Path) -> None:
    audio = wav(tmp_path / "a.wav", [100] * 160)
    result = cli("voice", "test", "--file", str(audio), cwd=tmp_path)
    assert result.code != 0
    result = cli("voice", "test", cwd=tmp_path)
    assert result.code != 0 and "interactive terminal" in result.stdout + result.stderr


def test_cli_voice_help(cli: Any, tmp_path: Path) -> None:
    out = cli("voice", "--help", cwd=tmp_path).stdout
    for sub in ("status", "setup", "model", "test"):
        assert sub in out
    assert "whisper.cpp" in out and "Vosk" not in out


def test_no_vosk_anywhere() -> None:
    import highhx.voice as package

    root = Path(package.__file__).parent
    for source in root.glob("*.py"):
        assert "vosk" not in source.read_text().lower(), source


# ============================================ /voice is a native command family
@pytest.fixture
def no_resolver_or_agent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tripwires on every route a request can take other than a slash command."""
    from highhx.agent.ui import TerminalUI

    reached: list[str] = []
    for owner, name in (
        (AgentREPL, "local_request"),  # the Free resolver
        (AgentREPL, "turn"),  # the AI agent
        (AgentREPL, "run_task"),
        (AgentREPL, "offer"),  # the Pro offer
        (TerminalUI, "capability_panel"),  # the capability gate's screen
    ):
        monkeypatch.setattr(owner, name, lambda *_a, _n=name, **_k: reached.append(_n))
    return reached


@pytest.mark.parametrize(
    "lines, shows",
    [
        (("/voice",), "🎙 Voice on"),
        (("/voice on",), "🎙 Voice on"),
        (("/voice on", "/voice off"), "🎙 Voice off."),
        (("/voice status",), "Speech-to-text"),
        (("/voice   STATUS",), "Speech-to-text"),
        (("/voice setup",), "Voice is ready"),
        (("/voice test", ""), "nothing was run"),
        (("highhx voice status",), "Speech-to-text"),
        (("highhx voice",), "🎙 Voice on"),
    ],
)
def test_voice_commands_never_reach_the_resolver_agent_or_pro_gate(
    agent_project: Path,
    make_app,
    machine: Machine,
    no_resolver_or_agent: list[str],
    lines: tuple[str, ...],
    shows: str,
) -> None:
    app = make_app(agent_project)
    manager = machine.manager()
    repl, buffer = session(app, *lines, "/quit", manager=manager)
    engines = manager.engines()
    engines.transcriber = FakeTranscriber("open GitHub")
    repl._voice._engines = engines  # type: ignore[union-attr]
    repl.run()
    out = buffer.getvalue()
    assert shows in out
    assert no_resolver_or_agent == []
    assert "HighhX Pro capability" not in out and "Nothing local maps" not in out
    assert "already in the HighhX session" not in out


def test_typing_voice_status_while_recording_runs_the_command_not_speech(
    agent_project: Path, make_app, machine: Machine, no_resolver_or_agent: list[str]
) -> None:
    """The v0.6.0 bug: the line typed to end a recording was dropped and the room noise that
    had been recorded went to the resolver ("HighhX Pro capability")."""
    app = make_app(agent_project)
    engines = ready_engines("we can play something in the south of the city")
    repl, buffer = session(app, "/voice", "", "/voice status", "/quit", engines=engines, manager=machine.manager())
    repl.run()
    out = buffer.getvalue()
    assert "Recording discarded — handling what you typed." in out
    assert "🎙 HighhX Voice" in out and "Speech-to-text" in out
    assert engines.transcriber.calls == 0  # type: ignore[union-attr]  # the recording was never transcribed
    assert no_resolver_or_agent == [] and "HighhX Pro capability" not in out


def test_voice_on_does_not_record_until_push_to_talk(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    engines = ready_engines("show git status")
    repl, buffer = session(app, "/voice on", "/voice status", "/voice", "/quit", engines=engines)
    repl.run()
    out = buffer.getvalue()
    assert "🎙 Listening..." not in out and engines.recorder.recordings == 0  # type: ignore[union-attr]
    assert "Voice is already on" in out  # /voice again does not toggle it off


def test_cli_voice_test_with_the_fixture_file(cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "voice" / "show_git_status.wav"
    seen: list[Path] = []

    class Whisper:
        name = "whisper.cpp"

        def transcribe(self, path: Path) -> Transcript:
            seen.append(path)
            return Transcript("Show Git status", 0.75)

    monkeypatch.setattr(
        "highhx.voice.voice_manager.VoiceManager.engines", lambda self: VoiceEngines(transcriber=Whisper())
    )
    data = cli("voice", "test", "--file", str(fixture), "--json", cwd=tmp_path).json()
    assert data == {"text": "Show Git status", "confidence": 0.75, "file": str(fixture)} and seen == [fixture]
    human = cli("voice", "test", "--file", str(fixture), cwd=tmp_path)
    assert human.code == 0 and "“Show Git status”" in human.stdout and "confidence 75%" in human.stdout
