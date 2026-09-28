"""The interactive HighhX session: one UI for Free and Pro, capabilities from the platform.

Covers the capability/entitlement layer, request routing without the AI agent, the
shared session (Free and Pro), Pro gating, platform failures, entitlement changes
during a session, the entry points (`highhx`, `highhx agent`) and TTY / non-TTY.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from highhx.agent import launch
from highhx.agent.input import BLOCK, read_request
from highhx.agent.repl import AgentREPL, nested_session
from highhx.agent.router import LocalAction, local_alternatives, required_capability, route
from highhx.agent.tools.base import ToolResult
from highhx.agent.ui import VIEW_PRO, TerminalUI
from highhx.cloud import capabilities, credentials
from highhx.cloud.account import Account, CloudAccount
from highhx.cloud.capabilities import LOCAL, PLATFORM, Capability, Connection, Entitlements
from highhx.cloud.plans import FREE, PLANS, PRO
from highhx.commands import App
from highhx.core.errors import AccountError, CloudError, ModelProviderError, PlanRequiredError
from highhx.ui.terminal import UNICODE_SYMBOLS
from tests.unit.agent.conftest import reply
from tests.unit.agent.test_cloud_and_cli import FakeClient, account_doc
from tests.unit.agent.test_repl import Script, make_ui, pro_account


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


@pytest.fixture
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.calls, FakeClient.routes = [], {}
    monkeypatch.setattr("highhx.cloud.account.PlatformClient", FakeClient)
    monkeypatch.delenv("HIGHHX_TOKEN", raising=False)
    monkeypatch.delenv("HIGHHX_API_URL", raising=False)
    return FakeClient


def git_init(root: Path) -> None:
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def free_repl(
    app: App,
    *lines: str,
    terminal: bool = False,
    entitlements: Entitlements | None = None,
    cloud: CloudAccount | None = None,
    factory: Callable[[], Any] | None = None,
) -> tuple[AgentREPL, io.StringIO, Script]:
    script = Script(*lines)
    ui, buffer = make_ui(script, terminal=terminal)
    repl = AgentREPL(
        None,
        ui,
        cloud,
        None,
        app=app,
        entitlements=entitlements or capabilities.local(),
        session_factory=factory,
        read_line=script,
    )
    return repl, buffer, script


# ------------------------------------------------------------ capabilities
def test_platform_capabilities_are_the_plan_features() -> None:
    assert {c.value for c in PLATFORM} == set(PLANS[PRO].features) - set(PLANS[FREE].features)
    assert LOCAL.isdisjoint(PLATFORM)


def test_signed_out_is_free_and_local() -> None:
    free = capabilities.local()
    assert free.tier == "Free" and free.status == "Free • Local" and not free.signed_in
    assert free.capabilities == LOCAL and not free.has(Capability.AI_AGENT)


def test_pro_account_gets_every_capability() -> None:
    pro = capabilities.from_account(Account.from_dict(account_doc(PRO)))
    assert pro.status == "Pro • Connected"
    assert pro.capabilities == LOCAL | PLATFORM


def test_free_account_is_connected_but_local() -> None:
    free = capabilities.from_account(Account.from_dict(account_doc(FREE)))
    assert free.status == "Free • Connected" and free.capabilities == LOCAL


def test_cached_account_is_offline() -> None:
    cached = capabilities.from_account(Account.from_dict(account_doc(PRO), cached=True))
    assert cached.status == "Pro • Offline (cached)" and cached.connection == Connection.CACHED


def test_the_platform_feature_list_wins_over_the_plan_name() -> None:
    """A document that says `pro` without the agent feature does not unlock the agent."""
    doc = {**account_doc(PRO), "features": ["cli"]}
    assert not capabilities.from_account(Account.from_dict(doc)).has(Capability.AI_AGENT)


def test_resolve_signed_out(fake_platform: type[FakeClient]) -> None:
    assert capabilities.resolve(CloudAccount()).status == "Free • Local"
    assert fake_platform.calls == []  # no network without an account


def test_resolve_pro(fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    assert capabilities.resolve(CloudAccount()).has(Capability.AI_COMPUTER_USE)


def test_resolve_platform_unavailable_keeps_local(fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    result = capabilities.resolve(CloudAccount())
    assert result.connection == Connection.UNAVAILABLE and result.capabilities == LOCAL
    assert result.status == "Free • Platform unavailable"
    assert "Local capabilities remain available" in (result.problem or "")


def test_resolve_rejected_token_keeps_local(fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = AccountError("Your sign-in expired.", hint="Run `highhx login`.")
    result = capabilities.resolve(CloudAccount())
    assert result.capabilities == LOCAL and "highhx login" in (result.problem or "")


# ------------------------------------------------------------------ routing
@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("run the tests", "project.test"),
        ("show git status", "git.status"),
        ("please build", "project.build"),
        ("security scan", "security.scan"),
    ],
)
def test_known_requests_run_deterministically(text: str, action: str) -> None:
    planned = route(text)
    assert planned.resolution is not None and planned.resolution.action == action and planned.capability is None


@pytest.mark.parametrize(
    ("text", "capability"),
    [
        ("Build a FastAPI authentication system.", Capability.AI_CODE_CHANGES),
        ("Fix the failing tests", Capability.AI_AGENT),  # debugging
        ("Refactor the authentication module.", Capability.AI_CODE_CHANGES),
        ("Add PostgreSQL support.", Capability.AI_CODE_CHANGES),
        ("Explain this repository.", Capability.AI_AGENT),
        ("Find why this API is returning 500.", Capability.AI_AGENT),
        ("Deploy this application.", Capability.AI_DEPLOY),
        ("commit and push my changes", Capability.AI_GIT),
        ("open the checkout page in the browser and fill in the form", Capability.AI_COMPUTER_USE),
    ],
)
def test_open_ended_requests_name_the_capability(text: str, capability: Capability) -> None:
    planned = route(text)
    assert planned.resolution is None and planned.capability == capability
    assert "HighhX Pro" in planned.reason


def test_local_alternatives_are_relevant() -> None:
    assert local_alternatives("Fix the failing tests")[0].action == "project.test"
    assert "deployment.deploy" in [a.action for a in local_alternatives("Deploy this application.")]
    assert [a.action for a in local_alternatives("Explain this repository.")] == ["project.detect", "project.status"]
    assert local_alternatives("Build a FastAPI authentication system.") == ()  # not `highhx build`
    assert len(local_alternatives("fix failing tests, lint, build, deploy, security, git")) <= 3


def test_every_local_alternative_is_a_real_action() -> None:
    from highhx.actions.catalog import default_catalog
    from highhx.agent import router

    catalog = default_catalog()
    actions = {action.action for _pattern, group in router._ALTERNATIVES for action in group}
    assert actions and all(name in catalog for name in actions), actions - set(catalog.names())


def test_required_capability_default() -> None:
    assert required_capability("hmm")[0] == Capability.AI_AGENT


# -------------------------------------------------------------------- input
def test_backslash_continuation_and_blocks() -> None:
    assert read_request(Script("first \\", "second"), prompt=">", continuation="…") == "first \nsecond"
    block = read_request(Script(BLOCK, "line 1", "  line 2", BLOCK), prompt=">", continuation="…")
    assert block == "line 1\n  line 2"
    with pytest.raises(EOFError):
        read_request(Script(), prompt=">", continuation="…")


def test_nested_sessions_are_refused() -> None:
    assert nested_session([]) and nested_session(["agent"]) and nested_session(["agent", "fix it"])
    assert not nested_session(["agent", "sessions"]) and not nested_session(["status"])


# -------------------------------------------------------------- Free session
def test_free_startup_and_known_request(agent_project: Path, make_app, capsys) -> None:
    git_init(agent_project)
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "show git status", "run the tests", "/quit")
    assert repl.run() == 0
    out = buffer.getvalue()
    assert "Developer command center" in out and "main • clean" in out and "Free • Local" in out
    assert "not available" in out  # the AI line tells the truth
    assert "◉ git status  git.status · safe" in out and "✓ git status" in out
    assert "◉ run the tests  project.test · low" in out and "✓ run the tests" in out  # a second request
    assert "passed" in capsys.readouterr().out  # the real command's own output
    assert "Bye." in out


def test_free_open_ended_request_offers_pro_and_local_alternatives(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "Fix the failing tests", "1", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "HighhX Pro capability" in out and "AI debugging requires HighhX Pro." in out
    assert "HighhX Pro can understand and execute this open-ended task." in out
    assert "Run the tests" in out and "project.test" in out
    assert "✓ Run the tests" in out  # continuing locally ran the real action
    assert "passed" in capsys.readouterr().out


def test_free_view_pro(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "open the dashboard in the browser and click export", "p", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "AI browser and desktop automation requires HighhX Pro." in out
    assert "HighhX Pro" in out and "highhx login" in out


def test_free_skip_does_nothing(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "Explain this repository.", "", "/quit")
    repl.run()
    assert "◉" not in buffer.getvalue()  # nothing ran


def test_capability_panel_answers() -> None:
    actions = (LocalAction("Run the tests", "project.test"), LocalAction("Diagnose", "security.diagnose"))
    for answer, expected in (("2", actions[1]), ("p", VIEW_PRO), ("", None), ("9", None)):
        ui, _buffer = make_ui(Script(answer))
        assert ui.capability_panel("AI code changes require HighhX Pro.", actions) == expected
    ui, _buffer = make_ui(Script())
    ui._interactive = False
    assert ui.capability_panel("x", actions) is None  # never blocks a non-interactive run


def test_free_shell_command_and_highhx_commands(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(
        app, "!echo hello-from-shell", "!", "highhx status --json", "highhx agent", "highhx nosuchcommand", "/quit"
    )
    repl.run()
    out, printed = buffer.getvalue(), capsys.readouterr()
    assert "✓ echo hello-from-shell" in out and "hello-from-shell" in printed.out
    assert "Usage: !<command>" in out
    assert "Project" in printed.out  # `highhx status` ran in the session…
    assert app.options.json is False  # …and a flag given to one command does not stick to the session
    assert "already in the HighhX session" in out
    assert "✗ highhx nosuchcommand — exit code 2" in out


def test_free_slash_commands(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(
        app, "/help", "/tools", "/status", "/context", "/memory", "/usage", "/plan", "", "/model", "", "y", "/exit"
    )
    repl.run()
    out = buffer.getvalue()
    assert "understands known actions without AI" in out
    assert "AI browser and desktop automation" in out and "available" in out and "HighhX Pro" in out
    assert "Free • Local" in out and "not signed in" in out
    assert "Files indexed" in out and "No project memory yet" in out
    assert "sign in with `highhx login`" in out
    assert "/plan <request> previews what it would run" in out
    assert "/model is part of the AI agent" in out
    assert "Nothing is waiting for an answer" in out


def test_free_history_and_config_run_real_commands(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/history", "/config", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "Nothing has run in this session yet" in out and "highhx history" in out
    assert "✓ highhx config show" in out
    assert "pyapp" in capsys.readouterr().out


def test_ctrl_c_during_a_local_command_cancels_only_that_command(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/quit")

    def interrupted() -> int:
        raise KeyboardInterrupt

    before = app.ctx.cancel
    assert repl._local("long job", interrupted) == 130
    assert "long job — cancelled" in buffer.getvalue()
    assert app.ctx.cancel is not before  # each request gets a fresh token; the app's own is untouched
    assert not before.cancelled
    assert repl.run() == 0  # the session continues


def test_ctrl_c_twice_and_ctrl_d_at_the_prompt(agent_project: Path, make_app) -> None:
    class Interrupting(Script):
        def __call__(self, prompt: str) -> str:
            raise KeyboardInterrupt

    app = make_app(agent_project)
    script = Interrupting()
    ui, buffer = make_ui(script)
    repl = AgentREPL(None, ui, None, app=app, read_line=script)
    assert repl.run() == 0 and "Press Ctrl+C again" in buffer.getvalue()
    repl2, buffer2, _ = free_repl(app)  # EOF straight away
    assert repl2.run() == 0 and "Bye." in buffer2.getvalue()


def test_dry_run_is_shown_and_respected(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project, dry_run=True)
    repl, buffer, _ = free_repl(app, "!touch created.txt", "/quit")
    repl.run()
    assert "Dry run" in buffer.getvalue()
    assert not (agent_project / "created.txt").exists()


def test_narrow_and_resized_terminals(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    for width in (40, 60, 100):
        script = Script("/status", "/tools", "/quit")
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=width, color_system=None, highlight=False)
        ui = TerminalUI(console, UNICODE_SYMBOLS, interactive=True, read_line=script)
        AgentREPL(None, ui, None, app=app, read_line=script).run()
        lines = [line for line in buffer.getvalue().splitlines() if "\x1b" not in line]
        assert all(len(line) <= width for line in lines), width
    # A resize is picked up by the next render: the same console, a new width.
    console.width = 50
    ui.print("x" * 10)
    assert console.width == 50


def test_terminal_prompt_is_readline_safe(agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    app = make_app(agent_project)
    seen: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: seen.append(prompt) or "done")
    ui = TerminalUI(Console(file=io.StringIO(), force_terminal=True), UNICODE_SYMBOLS, interactive=True)
    repl = AgentREPL(None, ui, None, app=app)
    assert repl._read() == "done"
    assert seen[0].startswith("\001\033[") and "\002❯" in seen[0]  # noqa: RUF001  (escapes hidden from readline)


# --------------------------------------------------------------- Pro session
def test_pro_session_uses_the_same_ui(agent_project: Path, make_session) -> None:
    script = Script("how many python files?", "/tools", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(
        agent_project, [reply("", [("list_files", {"glob": "*.py"})]), reply("Three.")], ui=ui
    )
    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "Developer command center" in out and "Pro • Connected" in out
    assert "✓ List *.py" in out and "Three." in out
    assert "Agent tools (" in out and "list_files" in out
    assert "HighhX Pro" not in out.split("capability", 1)[1].split("Agent tools", 1)[0]  # all available


def test_pro_session_local_commands_bypass_the_agent_ui(agent_project: Path, make_session, capsys) -> None:
    script = Script("!echo from-pro-shell", "/quit")
    ui, _buffer = make_ui(script)
    session, provider, _ = make_session(agent_project, [], ui=ui)
    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    assert "from-pro-shell" in capsys.readouterr().out
    assert provider.requests == []  # no model call for a shell command
    assert session.app.engine.output is session.sink  # the agent's routing is restored afterwards


def test_pro_platform_unavailable_falls_back_to_local(agent_project: Path, make_session, capsys) -> None:
    script = Script("run the tests", "y", "/status", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(
        agent_project, [ModelProviderError("Cannot reach the HighhX platform", connection=True)], ui=ui
    )
    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "Cannot reach the HighhX platform" in out
    assert "HighhX platform unavailable. Local capabilities remain available." in out
    assert any("Run run the tests locally instead (no AI)?" in p for p in script.prompts)
    assert "✓ run the tests" in out
    assert "Pro • Platform unavailable" in out  # /status tells the truth
    assert "passed" in capsys.readouterr().out


def test_pro_recovers_the_connection_status(agent_project: Path, make_session) -> None:
    script = Script("hi", "hi again", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(
        agent_project, [ModelProviderError("down", connection=True), reply("Back.")], ui=ui
    )
    repl = AgentREPL(session, ui, None, pro_account(), read_line=script)
    repl.run()
    assert repl.entitlements.connection == Connection.CONNECTED and "Back." in buffer.getvalue()


def test_upstream_failure_is_not_reported_as_the_platform_being_down(agent_project: Path, make_session) -> None:
    script = Script("explain this", "/status", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(agent_project, [ModelProviderError("invalid_model", status=400)], ui=ui)
    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "The AI request failed. Local capabilities remain available." in out
    assert "platform unavailable" not in out and "Pro • Connected" in out


def test_gateway_connection_failure_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.agent.model.base import ModelRequest
    from highhx.agent.model.platform import PlatformProvider

    class Down:
        def stream(self, *_args: Any, **_kwargs: Any) -> Any:
            raise CloudError("Cannot reach the HighhX platform at http://x (refused).")
            yield  # pragma: no cover

        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            return {}

    provider = PlatformProvider(Down())  # type: ignore[arg-type]
    with pytest.raises(ModelProviderError) as caught:
        list(provider.stream(ModelRequest("", [])))
    assert caught.value.connection is True


def test_plan_lapse_mid_session_switches_to_local(
    agent_project: Path, make_session, fake_platform: type[FakeClient]
) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    script = Script("Fix the failing tests", "", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(
        agent_project, [PlanRequiredError("The HighhX AI agent requires HighhX Pro.")], ui=ui
    )
    repl = AgentREPL(session, ui, CloudAccount(), pro_account(), read_line=script)
    repl.run()
    out = buffer.getvalue()
    assert "requires HighhX Pro" in out and "Free • Connected — continuing with local capabilities." in out
    assert "HighhX Pro capability" in out  # the same request, now handled locally
    assert repl.session is None and session.state == "closed"


def test_upgrade_during_a_session_attaches_the_agent(
    agent_project: Path, make_app, make_session, fake_platform: type[FakeClient]
) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    app = make_app(agent_project)
    cloud = CloudAccount()
    ui_holder: dict[str, Any] = {}

    def factory() -> Any:
        session, _provider, _ = make_session(agent_project, [reply("Agent here.")], ui=ui_holder["ui"])
        return session, Account.from_dict(account_doc(PRO)), False

    repl, buffer, _ = free_repl(
        app, "/tools", "hello", "/quit", entitlements=capabilities.resolve(cloud), cloud=cloud, factory=factory
    )
    ui_holder["ui"] = repl.ui
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)  # upgraded on the platform
    repl.refresh_entitlements()
    assert repl.session is not None and repl.entitlements.tier == "Pro"
    repl.run()
    out = buffer.getvalue()
    assert "Pro • Connected — the AI agent now handles your requests." in out
    assert "Agent here." in out


def test_tool_states_are_distinct() -> None:
    from highhx.agent.messages import ToolCall

    ui, buffer = make_ui(Script())
    call = ToolCall("c", "run_command", {})
    ui.tool_finished(None, call, ToolResult("x", ok=False, summary="blocked by policy", error_code="policy"), 0)
    ui.tool_finished(None, call, ToolResult("x", ok=False, summary="declined", error_code="denied"), 0)
    ui.tool_finished(None, call, ToolResult("x", ok=False, error_code="cancelled"), 0)
    ui.tool_finished(None, call, ToolResult("x", ok=False, summary="exit 1", error_code="failed"), 0)
    ui.tool_finished(None, call, ToolResult("ok", summary="42 passed"), 0)
    out = buffer.getvalue()
    assert "⊘ run_command — blocked by policy" in out and "⊘ run_command — declined" in out
    assert "○ run_command — cancelled" in out and "✗ run_command — exit 1" in out and "✓ run_command — 42 passed" in out


def test_approval_panels_render_on_any_console() -> None:
    from highhx.approvals.risk import RiskLevel
    from highhx.safety.confirmation import ConfirmationRequest

    ui, buffer = make_ui(Script("y", "y"))
    request = ConfirmationRequest(
        action="Push main to origin",
        target="origin/main",
        application="project",
        tool="git_push",
        risk=RiskLevel.CRITICAL,
        reasons=["pushes commits to a shared remote"],
        command="git push origin main",
        irreversible=True,
    )
    assert ui.confirm_action(request) is True
    assert ui.ask_permission("Edit a.py", []) == "yes"
    out = buffer.getvalue()
    assert "Approval required" in out and "critical" in out and "git push origin main" in out
    assert "Action requires approval" in out


# ------------------------------------------------------------------ launch
@pytest.fixture
def captured_repl(monkeypatch: pytest.MonkeyPatch) -> list[AgentREPL]:
    seen: list[AgentREPL] = []

    def fake_run(self: AgentREPL, first: str | None = None, *, resumed: bool = False) -> int:
        seen.append(self)
        self.first = first  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(AgentREPL, "run", fake_run)
    return seen


def test_launch_signed_out_is_free(agent_project: Path, make_app, captured_repl, fake_platform) -> None:
    assert launch.start_interactive(make_app(agent_project), first="run the tests") == 0
    repl = captured_repl[0]
    assert repl.session is None and repl.entitlements.status == "Free • Local"
    assert repl.first == "run the tests"  # type: ignore[attr-defined]


def test_launch_pro_attaches_the_agent(agent_project: Path, make_app, captured_repl, fake_platform) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    launch.start_interactive(make_app(agent_project))
    repl = captured_repl[0]
    assert repl.session is not None and repl.entitlements.status == "Pro • Connected"
    assert repl.session.provider.name == "highhx"  # always through the platform gateway
    repl.session.close()


def test_launch_platform_refusal_keeps_local(
    agent_project: Path, make_app, captured_repl, fake_platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PlanRequiredError("The HighhX AI agent requires HighhX Pro.")

    monkeypatch.setattr("highhx.agent.bootstrap.create_session", refuse)
    launch.start_interactive(make_app(agent_project))
    repl = captured_repl[0]
    assert repl.session is None and not repl.entitlements.has(Capability.AI_AGENT)
    assert "Local capabilities remain available" in (repl.entitlements.problem or "")


def test_launch_offline_without_cache(agent_project: Path, make_app, captured_repl, fake_platform) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    launch.start_interactive(make_app(agent_project))
    assert captured_repl[0].entitlements.status == "Free • Platform unavailable"


def test_interactive_terminal_rules(agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(agent_project)
    monkeypatch.setattr(type(app.output.console), "is_terminal", property(lambda _self: True))
    assert launch.interactive_terminal(app)
    for flag in ("json", "quiet"):
        other = make_app(agent_project, **{flag: True})
        assert not launch.interactive_terminal(other), flag
    assert not launch.interactive_terminal(make_app(agent_project, interactive=False))


# ------------------------------------------------------------- entry points
@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(launch, "interactive_terminal", lambda app: True)

    def fake_start(app: App, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(launch, "start_interactive", fake_start)
    return calls


def test_bare_highhx_in_a_terminal_starts_the_session(cli, tmp_path: Path, started) -> None:
    assert cli(cwd=tmp_path).code == 0 and started == [{}]


def test_highhx_agent_in_a_terminal_starts_the_same_session(cli, tmp_path: Path, started) -> None:
    assert cli("agent", "fix", "it", cwd=tmp_path).code == 0
    assert started[0]["first"] == "fix it" and started[0]["overrides"]["provider"] is None


def test_subcommands_and_flags_never_start_the_session(cli, tmp_path: Path, started) -> None:
    assert cli("status", "--json", cwd=tmp_path).code == 0
    assert cli("--version", cwd=tmp_path).stdout.startswith("highhx ")
    assert "Usage:" in cli("--help", cwd=tmp_path).stdout
    assert started == []


def test_non_tty_bare_highhx_keeps_help_and_json(cli, tmp_path: Path) -> None:
    result = cli(cwd=tmp_path)
    assert result.code == 0 and result.stdout.startswith("Usage:")
    json_run = cli("--json", cwd=tmp_path)
    assert json_run.code == 0
    assert json.loads(json_run.stdout[json_run.stdout.index("{") :]) == {"ok": True, "exit_code": 0}


# ---------------------------------------------------------------- real TTY
def _pty_session(
    tmp_path: Path, keys: list[bytes], *, cols: int, rows: int, resize: tuple[int, int] | None = None
) -> str:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import time

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.chdir(tmp_path)
        env = {k: v for k, v in os.environ.items() if k not in ("HIGHHX_NON_INTERACTIVE", "COLUMNS", "LINES")}
        env.update({"NO_COLOR": "1", "TERM": "xterm", "HIGHHX_BANNER": "compact"})
        os.execve(sys.executable, [sys.executable, "-m", "highhx"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    output = b""

    def pump(until: bytes, timeout: float = 30) -> None:
        nonlocal output
        deadline = time.monotonic() + timeout
        start = len(output)
        while time.monotonic() < deadline and until not in output[start:]:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    pump("❯".encode())  # noqa: RUF001
    for key in keys:
        if resize is not None and key == b"/status\r":
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", resize[1], resize[0], 0, 0))
        os.write(fd, key)
        pump(b"Bye." if key == b"\x04" else "❯".encode())  # noqa: RUF001
    # A session that has not exited (a key answered an unexpected prompt …) must fail the
    # test with its output, not block CI forever in waitpid.
    deadline = time.monotonic() + 15
    while os.waitpid(pid, os.WNOHANG) == (0, 0):
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            output += b"\n[the session did not exit; killed by the test]\n"
            break
        pump(b"\0", timeout=0.2)
    os.close(fd)
    return output.decode("utf-8", "replace").replace("\r\n", "\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_real_terminal_session_free(tmp_path: Path) -> None:
    text = _pty_session(tmp_path, [b"/tools\r", b"Explain this repository.\r", b"\r", b"\x04"], cols=90, rows=30)
    assert "HighhX v" in text and "Developer command center" in text and "Free • Local" in text
    assert "AI browser and desktop automation" in text and "HighhX Pro capability" in text
    assert "Usage:" not in text  # the session, not the help page
    assert "Bye." in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_real_terminal_narrow_and_resized(tmp_path: Path) -> None:
    text = _pty_session(tmp_path, [b"/status\r", b"\x04"], cols=100, rows=30, resize=(44, 30))
    after = text.split("/status", 1)[1]
    assert "Free • Local" in after
    assert all(len(line) <= 44 for line in after.splitlines() if "\x1b" not in line)
