"""Pro's hybrid model (the agent proposes action graphs; HighhX executes them), plugin action
boundaries, voice, and the Free/Pro boundary for everything new."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.catalog import catalog_for, default_catalog, plugin_actions
from highhx.actions.policy import Risk
from highhx.agent.permissions import ApprovalMode
from highhx.agent.repl import AgentREPL
from highhx.agent.tools.actions import RunActionsTool
from highhx.cloud.plans import FREE, PLANS, PRO
from highhx.plugins.manifest import PluginCommand
from tests.unit.agent.conftest import reply
from tests.unit.agent.test_free_pro_boundary import tripwires  # noqa: F401
from tests.unit.agent.test_interactive_shell import free_repl
from tests.unit.agent.test_repl import Script, make_ui


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


def run_actions(*items: dict[str, Any], rollback: bool = False) -> list[Any]:
    return reply("", [("run_actions", {"actions": list(items), "rollback_on_failure": rollback})])


# ------------------------------------------------------------- run_actions
def test_the_agent_executes_graphs_through_the_same_engine(agent_project: Path, make_session) -> None:
    session, provider, ui = make_session(
        agent_project,
        [
            run_actions(
                {"id": "w", "action": "filesystem.write", "inputs": {"path": "NOTES.md", "content": "n\n"}},
                {"id": "t", "action": "project.test", "depends_on": ["w"]},
            ),
            reply("Wrote notes and ran the tests."),
        ],
    )
    result = session.run_turn("write notes then test")
    assert result.stopped == "completed" and (agent_project / "NOTES.md").exists()
    assert [r.risk_name for r in ui.requests] == ["medium"]  # the write was approved by the person
    assert ui.of("permission") == ["project.test · highhx test"]  # low risk: the agent asks in ask mode
    content = provider.last_tool_results()[0].content
    assert '"ok": true' in content and "untrusted" in content.lower()
    assert result.changed_files == ["NOTES.md"]
    assert session.undo() == ["NOTES.md"] and not (agent_project / "NOTES.md").exists()  # /undo works


def test_the_agent_cannot_preapprove_even_with_yes(agent_project: Path, make_session) -> None:
    session, provider, _ui = make_session(
        agent_project,
        [run_actions({"action": "filesystem.delete", "inputs": {"path": "pyproject.toml"}}), reply("ok")],
        yes=True,
        interactive=False,
    )
    session.run_turn("delete it")
    assert (agent_project / "pyproject.toml").exists()
    assert "Confirmation required" in provider.last_tool_results()[0].content


def test_rollback_on_failure_undoes_completed_items(agent_project: Path, make_session) -> None:
    session, provider, _ui = make_session(
        agent_project,
        [
            run_actions(
                {"id": "a", "action": "filesystem.write", "inputs": {"path": "a.txt", "content": "a"}},
                {"id": "b", "action": "filesystem.read", "inputs": {"path": "missing.txt"}, "depends_on": ["a"]},
                rollback=True,
            ),
            reply("rolled back"),
        ],
    )
    session.run_turn("try")
    data = provider.last_tool_results()[0].content
    assert not (agent_project / "a.txt").exists() and "removed a.txt" in data


def test_read_only_mode_allows_only_safe_actions(agent_project: Path, make_session) -> None:
    session, provider, _ui = make_session(
        agent_project,
        [
            run_actions(
                {"id": "r", "action": "filesystem.read", "inputs": {"path": "pyproject.toml"}},
                {"id": "w", "action": "filesystem.write", "inputs": {"path": "x.txt", "content": "x"}},
            ),
            reply("done"),
        ],
        mode=ApprovalMode.READ_ONLY,
    )
    session.run_turn("look")
    content = provider.last_tool_results()[0].content
    assert "read-only" in content and not (agent_project / "x.txt").exists()


@pytest.mark.parametrize(
    ("action", "error"),
    [
        ("browser.open", "use the computer-use tools"),
        ("nope.nope", "unknown action"),
    ],
)
def test_the_agent_cannot_reach_ui_or_unknown_actions(
    agent_project: Path, make_session, action: str, error: str
) -> None:
    session, provider, _ui = make_session(
        agent_project, [run_actions({"action": action, "inputs": {"url": "https://x"}}), reply("ok")]
    )
    session.run_turn("go")
    assert error in provider.last_tool_results()[0].content


def test_plan_features_gate_the_agents_actions() -> None:
    limited = RunActionsTool(frozenset({"agent", "agent.commands"}))
    assert "project.test" in limited.allowed and "filesystem.write" not in limited.allowed
    assert "filesystem.write" not in limited.description and "git.push" not in limited.description
    full = RunActionsTool(PLANS[PRO].features)
    assert {"filesystem.write", "git.push", "deployment.deploy"} <= set(full.allowed)
    assert not any(n.startswith(("browser.", "computer.", "plugin.")) for n in full.allowed)


def test_run_actions_is_not_a_computer_tool_for_the_platform() -> None:
    """The platform entitles computer use by tool name; run_actions must never carry UI actions."""
    from highhx.cloud.plans import AGENT_COMPUTER_USE

    tool = RunActionsTool(PLANS[PRO].features)
    assert "browser." not in tool.description and "computer.launch" not in tool.description
    assert all(spec.feature != AGENT_COMPUTER_USE for spec in tool.allowed.values())


def test_the_agent_sees_run_actions(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(agent_project, [reply("hi")])
    session.run_turn("hello")
    names = [t.name for t in provider.requests[0].tools]
    assert "run_actions" in names and "computer_act" in names


# ------------------------------------------------------------------ plugins
class FakeManifest:
    name = "acme"
    version = "1.2.0"


class FakeRegistry:
    def __init__(self, risk: str) -> None:
        self.declarative_commands = {
            "lint-docs": (FakeManifest(), PluginCommand("lint-docs", "echo docs ok", risk=risk))
        }


def test_plugin_commands_become_bounded_actions(agent_project: Path, make_app, monkeypatch) -> None:
    app = make_app(agent_project)
    monkeypatch.setattr(type(app), "plugins", property(lambda self: FakeRegistry("safe")))
    (spec,) = plugin_actions(app)
    assert spec.name == "plugin.acme.lint-docs" and spec.risk == Risk.LOW  # "safe" cannot lower the floor
    assert spec.agent is False and spec.policy_name({}) == "plugin:acme:lint-docs"
    assert spec.command_for({"args": ["--strict"]}) == "echo docs ok --strict"
    assert "plugin.acme.lint-docs" in catalog_for(app)
    assert "plugin.acme.lint-docs" not in default_catalog()
    assert all(not s.name.startswith("plugin.") for s in catalog_for(app).for_agent(PLANS[PRO].features))


def test_plugin_declared_risk_only_raises(agent_project: Path, make_app, monkeypatch) -> None:
    app = make_app(agent_project)
    monkeypatch.setattr(type(app), "plugins", property(lambda self: FakeRegistry("critical")))
    assert plugin_actions(app)[0].risk == Risk.CRITICAL


def test_broken_plugins_do_not_break_the_catalog(agent_project: Path, make_app, monkeypatch) -> None:
    from highhx.core.errors import PluginError

    app = make_app(agent_project)

    def broken(self: Any) -> Any:
        raise PluginError("bad manifest")

    monkeypatch.setattr(type(app), "plugins", property(broken))
    assert catalog_for(app) is default_catalog()


# -------------------------------------------------------------------- voice
class FakeRecorder:
    name = "fake-mic"

    def record(self, path: Path, stop: Any) -> None:
        stop.wait(5)
        path.write_bytes(b"RIFF" + b"\0" * 100)


class FakeTranscriber:
    name = "fake-stt"

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    def transcribe(self, path: Path) -> str:
        assert path.exists()
        return self.texts.pop(0)


class FakeSpeaker:
    name = "fake-tts"

    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def stop(self) -> None:
        return None


def voice_repl(
    app: Any, *lines: str, heard: tuple[str, ...] = (), listen: bool = True
) -> tuple[AgentREPL, Any, FakeSpeaker]:
    from highhx.voice.engines import VoiceEngines
    from highhx.voice.mode import VoiceMode

    script = Script(*lines)
    ui, buffer = make_ui(script)
    speaker = FakeSpeaker()
    engines = VoiceEngines(
        recorder=FakeRecorder() if listen else None,
        transcriber=FakeTranscriber(*heard) if listen else None,
        speaker=speaker,
    )
    repl = AgentREPL(
        None, ui, None, app=app, read_line=script, voice=True, voice_mode=VoiceMode(ui, app.ctx.events, engines=engines)
    )
    return repl, buffer, speaker


def test_voice_feeds_the_same_pipeline(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    # Enter (talk) → Enter (stop) → confirm → handled like typed text → spoken outcome
    repl, buffer, speaker = voice_repl(app, "", "", "y", "/quit", heard=("run the tests",))
    repl.run()
    out = buffer.getvalue()
    assert "🎙 Voice on" in out and "Heard:" in out and "run the tests" in out
    assert "◉ run the tests  project.test · low" in out
    assert speaker.said == ["run the tests: done."]


def test_voice_transcripts_must_be_confirmed(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, speaker = voice_repl(app, "", "", "n", "/quit", heard=("delete everything",))
    repl.run()
    assert "Not run." in buffer.getvalue() and "◉" not in buffer.getvalue() and speaker.said == []


def test_voice_edit_corrects_the_transcript(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = voice_repl(app, "", "", "e", "show git status", "/quit", heard=("so get stay tus",))
    repl.run()
    assert "◉ git status" in buffer.getvalue()


def test_voice_on_free_speaks_the_pro_boundary(agent_project: Path, make_app, tripwires: list[str]) -> None:  # noqa: F811
    app = make_app(agent_project)
    repl, buffer, speaker = voice_repl(app, "", "", "y", "", "/quit", heard=("fix the failing tests",))
    repl.run()
    assert "HighhX Pro capability" in buffer.getvalue()
    assert speaker.said == ["AI debugging requires HighhX Pro."]
    assert tripwires == []  # voice on Free touches no AI


def test_voice_without_speech_to_text_is_honest(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, speaker = voice_repl(app, "run the tests", "/voice status", "/voice mute", "/quit", listen=False)
    repl.run()
    out = buffer.getvalue()
    assert "Listening is off" in out and "fake-tts" in out
    assert speaker.said == ["run the tests: done."]


def test_voice_unavailable(agent_project: Path, make_app) -> None:
    from highhx.voice.engines import VoiceEngines
    from highhx.voice.mode import VoiceMode

    app = make_app(agent_project)
    ui, buffer = make_ui(Script())
    mode = VoiceMode(ui, app.ctx.events, engines=VoiceEngines(notes=["no recorder found"]))
    assert mode.enable() is False and "Voice is not available" in buffer.getvalue()


def test_spoken_form() -> None:
    from highhx.voice.mode import spoken

    assert spoken("**Done.** Fixed `cart.py`.\n```\nlong code\n```\nAll 48 tests pass.") == (
        "Done. Fixed cart.py. All 48 tests pass."
    )


def test_voice_check_command(cli, tmp_path: Path) -> None:
    data = cli("voice", "--check", "--json", cwd=tmp_path).json()
    assert set(data) >= {"recorder", "speech-to-text", "speech", "can_listen", "can_speak", "notes"}


# ------------------------------------------------------------ Free boundary
def test_free_actions_workflows_and_tools_never_touch_ai(
    agent_project: Path,
    make_app,
    tripwires: list[str],  # noqa: F811
) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(
        app,
        "run the tests",
        "/plan show git status",
        "/approve",
        "/run filesystem.read path=pyproject.toml",
        "/tools",
        "/workflows",
        "!echo hi",
        "/quit",
    )
    repl.run()
    assert tripwires == [] and repl.session is None
    assert "Unexpected error" not in buffer.getvalue()


def test_free_package_imports_nothing_for_ai_when_using_actions() -> None:
    """The action engine, resolver, workflows and voice import no provider SDK."""
    import subprocess
    import sys

    code = (
        "import sys, highhx.actions.executor, highhx.actions.resolver, highhx.actions.catalog, "
        "highhx.workflows.engine, highhx.voice.mode, highhx.agent.repl;"
        "print([m for m in sys.modules if m.split('.')[0] in ('anthropic','openai') or m.startswith('google.genai')])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_plans_are_unchanged_by_the_action_engine() -> None:
    """Free stays deterministic: the platform features did not grow for Free."""
    assert PLANS[FREE].features == frozenset({"cli"})
    assert json.dumps(sorted(PLANS[PRO].features))
