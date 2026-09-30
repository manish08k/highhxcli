"""The Free/Pro automation boundary.

Free: text or voice → the existing router → the deterministic decision → plan → action
executor → automation bridge (C#/.NET or Python engine). No model, no JEv, no AI service.
Pro: text or voice → the router → the Pro agent (the only advanced-reasoning capability
behind the JEv gate) → structured actions → the same executor and the same bridge.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.repl import AgentREPL
from highhx.automation.engine.bridge import AutomationBridge
from highhx.cloud import capabilities
from highhx.cloud.capabilities import Capability
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from highhx.core.errors import PlanRequiredError
from highhx.decision.advanced import ADVANCED_REASONING, advanced_reasoning_available, require_advanced_reasoning
from highhx.safety.actions import Actor
from tests.unit.agent.conftest import reply
from tests.unit.agent.test_free_pro_boundary import tripwires  # noqa: F401
from tests.unit.agent.test_repl import Script, make_ui, pro_account
from tests.unit.automation.fakes import FakeEngine
from tests.unit.voice.fakes import FakeRecorder, FakeSpeaker, FakeTranscriber

AI_STACK = (
    "highhx.agent.session",
    "highhx.agent.bootstrap",
    "highhx.agent.model.platform",
    "highhx.agent.model.anthropic",
    "highhx.agent.model.openai",
    "highhx.agent.model.gemini",
    "highhx.decision.advanced",
)
"""The agent runtime, every model provider and the JEv gate. (``agent.model.base``/``registry`` —
shared type and metadata modules imported by ``actions.spec`` since the action engine — are
not the AI stack: nothing in them is called on Free, which the tripwire tests prove.)"""
VENDOR_SDKS = ("anthropic", "openai", "google.genai", "google.generativeai")


# ------------------------------------------------------------------ Free
def test_the_free_automation_path_imports_no_ai(tmp_path: Path) -> None:
    """Deciding, planning, verifying and the bridge load neither the agent runtime, a model
    provider, the JEv gate nor a vendor SDK — in a fresh interpreter, so nothing else has
    imported them first."""
    code = f"""
import sys
from pathlib import Path
from highhx.actions.resolver import ResolverContext
from highhx.decision.deterministic import DeterministicDecider
from highhx.language.targets import default_registry
import highhx.plans.request, highhx.plans.runner, highhx.verification.strategies
import highhx.automation.engine.bridge, highhx.automation.engine.python_engine, highhx.automation.engine.provider
decider = DeterministicDecider(ResolverContext(root=Path({str(tmp_path)!r}), _targets=default_registry()))
for text in ("open Gmail and search internship", "play lofi on YouTube", "switch to Slack", "run the tests"):
    assert decider.decide(text).route == "local", text
loaded = sorted(m for m in sys.modules if m.startswith({AI_STACK!r}) or m.split(".")[0] in {VENDOR_SDKS!r})
print(loaded)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]", result.stdout


def test_free_computer_automation_touches_no_ai(
    agent_project: Path,
    make_app: Any,
    tripwires: list[str],  # noqa: F811
    browser: dict[str, Any],
    engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("highhx.computer.desktop._platform_key", lambda: "darwin")  # macOS app names
    app = make_app(agent_project)
    script = Script("open GitHub and search for highhx", "switch to Safari", "open spotifyy", "/quit")
    ui, buffer = make_ui(script)
    AgentREPL(None, ui, None, app=app, read_line=script).run()
    out = buffer.getvalue()
    assert tripwires == []  # no agent, provider, gateway or key lookup
    assert browser["flows"] == [{"open": "https://github.com"}, {"open": "https://github.com/search?q=highhx"}]
    assert engine.sent("focus") == [("focus", {"app": "Safari"})]  # through the automation bridge
    assert "I don't know this action yet" in out and "Nothing ran" in out


def test_jev_and_advanced_reasoning_are_pro_only() -> None:
    free = capabilities.local()
    assert not advanced_reasoning_available(free)
    with pytest.raises(PlanRequiredError, match="HighhX Pro"):
        require_advanced_reasoning(free)
    pro = dataclasses.replace(free, capabilities=free.capabilities | {ADVANCED_REASONING})
    assert advanced_reasoning_available(pro) and ADVANCED_REASONING == Capability.AI_AGENT
    require_advanced_reasoning(pro)  # granted by the platform: no error


def test_a_cached_pro_account_grants_no_advanced_reasoning(agent_project: Path, make_app: Any) -> None:
    """Only a live platform answer attaches the agent; a local file claiming Pro grants nothing."""
    cached = dataclasses.replace(pro_account(), cached=True)
    assert not advanced_reasoning_available(capabilities.from_account(cached))


# ------------------------------------------------------------------- Pro
def test_pro_requests_reach_the_advanced_reasoning_path(agent_project: Path, make_session: Any) -> None:
    script = Script("open Gmail and search internship", "/quit")
    ui, buffer = make_ui(script)
    session, provider, _ = make_session(agent_project, [reply("I'd open Gmail and search for internship.")], ui=ui)
    AgentREPL(session, ui, cloud=None, account=pro_account(), read_line=script).run()  # type: ignore[arg-type]
    assert len(provider.requests) == 1  # the agent (the Pro reasoning path) handled it
    assert "open Gmail and search internship" in str(provider.requests[0].messages[-1])
    assert "◉ open Gmail" not in buffer.getvalue()  # not the Free deterministic runner


def test_pro_agent_desktop_actions_run_through_the_common_bridge(
    agent_project: Path, make_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEngine()
    driver = HighhXDriver(AutomationBridge(fake))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    monkeypatch.setattr(sys, "platform", "darwin")
    steps = [
        reply("", [("computer_observe", {"source": "desktop"})]),
        reply("", [("computer_act", {"source": "desktop", "action": "click:a1"})]),
        reply("Saved."),
    ]
    session, _provider, _ui = make_session(agent_project, steps)
    result = session.run_turn("save the document")
    assert result.stopped == "completed"
    assert fake.sent("inspect") and fake.sent("click") == [
        ("click", {"name": "Save", "role": "button", "index": 0, "bounds": [100, 100, 80, 30]})
    ]


def test_free_actions_and_pro_tools_share_one_bridge_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.automation.engine import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "engine_binary", lambda: None)
    monkeypatch.delenv(bridge_module.ENGINE_ENV, raising=False)
    engine = type("Engine", (), {"run": lambda *a, **k: None})()
    session = ComputerSession(type("Gate", (), {"engine": engine})(), actor=Actor.AGENT)  # type: ignore[arg-type]
    monkeypatch.setattr(sys, "platform", "darwin")
    provider = session.provider("desktop")
    assert provider.driver is session.driver()  # type: ignore[attr-defined]
    assert session.driver().name == "python"  # no .NET engine installed here → the built-in engine
    session.close()


# ------------------------------------------------------------------ voice
def _voice_repl(app: Any, session: Any, heard: tuple[str, ...], *lines: str) -> tuple[AgentREPL, Any, FakeSpeaker]:
    from highhx.voice.engines import VoiceEngines
    from highhx.voice.mode import VoiceMode

    script = Script(*lines)
    ui, buffer = make_ui(script)
    speaker = FakeSpeaker()
    engines = VoiceEngines(recorder=FakeRecorder(), transcriber=FakeTranscriber(*heard), speaker=speaker)
    repl = AgentREPL(
        session,
        ui,
        None,
        account=pro_account() if session is not None else None,
        app=app,
        read_line=script,
        voice=True,
        voice_mode=VoiceMode(ui, app.ctx.events, engines=engines),
    )
    return repl, buffer, speaker


def test_voice_on_free_is_the_same_automation_pipeline(
    agent_project: Path,
    make_app: Any,
    tripwires: list[str],  # noqa: F811
    browser: dict[str, Any],
) -> None:
    app = make_app(agent_project)
    repl, buffer, speaker = _voice_repl(app, None, ("open GitHub",), "", "", "/quit")
    repl.run()
    assert browser["flows"] == [{"open": "https://github.com"}]  # the same executor and verification
    assert "↳ verified" in buffer.getvalue() and speaker.said == ["open GitHub: done."]
    assert tripwires == []


def test_voice_never_runs_what_it_does_not_know(
    agent_project: Path, make_app: Any, browser: dict[str, Any], engine: FakeEngine
) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = _voice_repl(app, None, ("open spotifyy then press enter",), "", "", "/quit")
    repl.run()
    assert "I don't know this action yet" in buffer.getvalue()
    assert browser["flows"] == [] and engine.sent("key", "type", "hotkey", "click") == []


def test_voice_on_pro_goes_to_the_agent(agent_project: Path, make_app: Any, make_session: Any) -> None:
    session, provider, _ = make_session(agent_project, [reply("On it.")])
    repl, _buffer, _ = _voice_repl(session.app, session, ("open github",), "", "", "/quit")
    repl.run()
    assert len(provider.requests) == 1  # the same router: a Pro session sends it to the agent


# ------------------------------------------------------------- ambiguity
@pytest.mark.parametrize(
    "text",
    [
        "open it",
        "open that",
        "type my password",
        "do the usual",
        "open the admin page in the browser and fill in the form",
    ],
)
def test_ambiguous_requests_run_nothing_unverified(
    agent_project: Path, make_app: Any, browser: dict[str, Any], engine: FakeEngine, text: str
) -> None:
    app = make_app(agent_project)
    script = Script(text, "", "/quit")
    ui, buffer = make_ui(script)
    AgentREPL(None, ui, None, app=app, read_line=script).run()
    out = buffer.getvalue()
    assert engine.sent("key", "type", "hotkey", "click") == []
    assert "✓" not in out or "verified" in out  # nothing is reported done without verification
