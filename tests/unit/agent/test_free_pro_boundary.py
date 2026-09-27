"""The Free / Pro boundary.

Free is deterministic: it never calls a model, never reads or accepts a provider API key
(no bring-your-own-key), and never constructs the agent runtime. Every AI request goes
through the HighhX platform, which decides; the CLI only attaches the agent when the
platform itself confirms it — never on the strength of local data.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.agent import launch
from highhx.agent.repl import AgentREPL, command_words, nested_session
from highhx.cloud import capabilities, credentials
from highhx.cloud.account import Account, CloudAccount
from highhx.cloud.capabilities import LOCAL, Capability, Connection
from highhx.cloud.plans import FREE, PLANS, PRO
from highhx.computer.intents import looks_like_file, parse
from highhx.core.errors import CloudError
from tests.unit.agent.conftest import reply
from tests.unit.agent.test_cloud_and_cli import FakeClient, account_doc
from tests.unit.agent.test_interactive_shell import free_repl
from tests.unit.agent.test_repl import Script, make_ui, pro_account

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
SPEC_REQUESTS = (
    "Build a FastAPI authentication system.",
    "Fix the failing tests.",
    "Explain this repository.",
    "Add PostgreSQL support.",
    "Refactor the authentication module.",
    "Find why this API is returning 500.",
    "Deploy this application.",
    "open the admin page in the browser and click export",
    "commit and push my changes",
    "open main.py",
)


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


@pytest.fixture
def tripwires(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any use of the AI stack (recorded, not raised: the session swallows errors to stay up)."""
    hits: list[str] = []

    def wire(name: str) -> Any:
        def tripped(*_args: Any, **_kwargs: Any) -> Any:
            hits.append(name)
            raise AssertionError(f"Free touched {name}")

        return tripped

    from highhx.agent import bootstrap, session
    from highhx.agent.model import platform, registry

    monkeypatch.setattr(session.AgentSession, "__init__", wire("AgentSession"))
    monkeypatch.setattr(platform.PlatformProvider, "__init__", wire("PlatformProvider"))
    monkeypatch.setattr(platform.PlatformProvider, "stream", wire("PlatformProvider.stream"))
    monkeypatch.setattr(bootstrap, "create_session", wire("create_session"))
    monkeypatch.setattr(bootstrap, "build_provider", wire("build_provider"))
    monkeypatch.setattr(registry, "create_direct_provider", wire("create_direct_provider"))
    monkeypatch.setattr(registry, "api_key_for", wire("api_key_for"))
    for key in PROVIDER_KEYS:
        monkeypatch.setenv(key, "sk-should-never-be-used-000000")  # highhx:allow-secret (test fixture)
    return hits


# ------------------------------------------------- Free never touches the AI stack
def test_free_session_never_touches_the_ai_stack(agent_project: Path, make_app, tripwires: list[str], capsys) -> None:
    app = make_app(agent_project)
    lines: list[str] = []
    for request in SPEC_REQUESTS:
        lines += [request, ""]  # every open-ended request → the Pro panel, skipped
    lines += [
        "run the tests",
        "show git status",
        "!echo local",
        "highhx status",
        "highhx agent models",
        "highhx agent fix it",
        "highhx -v agent fix it",
        "highhx -C . agent",
        "highhx -v",
        "/model",
        "",
        "/model anthropic",
        "",
        "/mode auto-edit",
        "",
        "/plan",
        "",
        "/undo",
        "",
        "/usage",
        "/tools",
        "/status",
        "/context",
        "/quit",
    ]
    repl, buffer, _ = free_repl(app, *lines)
    assert repl.run() == 0
    out = buffer.getvalue()
    assert tripwires == []  # no runtime, provider, gateway or key lookup
    assert repl.session is None
    assert out.count("HighhX Pro capability") >= len(SPEC_REQUESTS) + 5
    assert out.count("already in the HighhX session") == 4
    assert "Unexpected error" not in out


def test_tripwires_are_live_for_pro(agent_project: Path, make_app, tripwires: list[str], fake_platform) -> None:
    """Positive control: the same launch path reaches the (tripwired) runtime constructor as soon as
    the platform grants the agent — so the Free test above would have caught a leak."""
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    with pytest.raises(AssertionError, match="Free touched create_session"):
        launch.start_interactive(make_app(agent_project))
    assert tripwires == ["create_session"]


def test_every_request_that_needs_reasoning_goes_to_the_pro_panel() -> None:
    from highhx.agent.router import route

    for request in SPEC_REQUESTS:
        planned = route(request)
        assert planned.intent is None and planned.capability is not None, request
        assert "HighhX Pro" in planned.reason


def test_free_one_shot_agent_never_constructs_the_runtime(
    cli, tmp_path: Path, fake_platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.agent import session
    from highhx.agent.model import platform

    built: list[str] = []
    monkeypatch.setattr(session.AgentSession, "__init__", lambda *a, **k: built.append("session"))
    monkeypatch.setattr(platform.PlatformProvider, "__init__", lambda *a, **k: built.append("provider"))
    assert cli("agent", "fix my tests", cwd=tmp_path).code == 10  # signed out
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    assert cli("agent", "fix my tests", cwd=tmp_path).code == 10  # signed in on Free
    assert built == []


def test_the_cli_package_has_no_byok_path() -> None:
    """Static proof: outside the provider adapters (used by the platform server) and their
    registry, nothing in the CLI constructs a direct provider, reads a provider key or
    imports a vendor SDK."""
    import ast

    import highhx

    root = Path(highhx.__file__).parent
    allowed = {root / "agent" / "model" / name for name in ("anthropic.py", "openai.py", "gemini.py", "registry.py")}
    allowed.add(root / "agent" / "model" / "__init__.py")  # re-exports for the server
    offenders = []
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in ("create_direct_provider", "api_key_for"):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} {name}")
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] in ("anthropic", "openai") or m.startswith("google.genai") for m in modules):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} import {modules}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PROVIDER_KEYS:
                offenders.append(f"{path.relative_to(root)}:{node.lineno} {node.value}")
    assert offenders == []


def test_build_provider_only_ever_returns_the_platform_gateway(fake_platform) -> None:
    from highhx.agent.bootstrap import build_provider
    from highhx.agent.model.platform import PlatformProvider
    from highhx.agent.model.registry import register_provider
    from highhx.agent.settings import AgentSettings
    from highhx.core.errors import ConfigError

    credentials.save(credentials.Credentials(token="hhx_t"))
    account = Account.from_dict(account_doc(PRO))
    for name in ("highhx", "anthropic", "openai", "gemini"):
        assert isinstance(build_provider(AgentSettings(provider=name), CloudAccount(), account), PlatformProvider)
    register_provider("byok-plugin", lambda key: object())  # type: ignore[arg-type,return-value]
    with pytest.raises(ConfigError):  # a plugin-registered direct provider is not selectable
        build_provider(AgentSettings(provider="byok-plugin"), CloudAccount(), account)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_free_process_imports_no_sdk_and_reads_no_provider_key(agent_project: Path, tmp_path: Path) -> None:
    """A fresh interpreter runs a Free session; every environment lookup is recorded."""
    script = textwrap.dedent(
        f"""
        import io, json, os, sys
        class Recording(dict):
            seen = set()
            def __getitem__(self, k): Recording.seen.add(k); return super().__getitem__(k)
            def get(self, k, d=None): Recording.seen.add(k); return super().get(k, d)
            def __contains__(self, k): Recording.seen.add(k); return super().__contains__(k)
            def copy(self): return dict(self)
        os.environ = Recording(os.environ)
        from rich.console import Console
        from highhx.agent.repl import AgentREPL
        from highhx.agent.ui import TerminalUI
        from highhx.commands import App
        from highhx.core.context import Options
        from highhx.ui.terminal import UNICODE_SYMBOLS
        from highhx.agent import launch
        lines = {json.dumps([*SPEC_REQUESTS, "run the tests", "!echo hi", "/tools", "/model", "", "/quit"])}
        def read(_prompt):
            if not lines: raise EOFError
            return lines.pop(0)
        import highhx.agent.repl as r
        r._setup_readline = lambda: (lambda: None)
        app = App(Options(interactive=True), cwd=__import__("pathlib").Path({str(agent_project)!r}))
        ui = TerminalUI(Console(file=io.StringIO()), UNICODE_SYMBOLS, interactive=True, read_line=read)
        repl = AgentREPL(None, ui, app.cloud, app=app, read_line=read)
        repl.run()
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("anthropic", "openai") or m.startswith("google.genai"))
        print(json.dumps({{"keys": sorted(k for k in Recording.seen if k in {list(PROVIDER_KEYS)!r}), "sdks": loaded,
                           "session": repl.session is not None, "recorder_live": "HIGHHX_DATA_DIR" in Recording.seen}}))
        """
    )
    env = dict(__import__("os").environ)
    env.update(dict.fromkeys(PROVIDER_KEYS, "sk-never-read-00000000"))  # highhx:allow-secret (test fixture)
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300, check=False
    )
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"keys": [], "sdks": [], "session": False, "recorder_live": True}


# ------------------------------------------ the agent attaches only on the platform's word
def _tampered_cache() -> None:
    doc = {**account_doc(PRO), "user": {"id": "u", "email": "free@example.com"}}
    credentials.save(credentials.Credentials(token="hhx_free_user", account=doc, account_fetched_at=time.time()))


def test_edited_cache_while_offline_grants_nothing(fake_platform: type[FakeClient]) -> None:
    _tampered_cache()
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    result = capabilities.resolve(CloudAccount())
    assert result.capabilities == LOCAL and result.connection == Connection.CACHED
    assert result.status == "Pro • Offline (cached)"  # labelled as a cached, offline plan …
    assert not result.has(Capability.AI_AGENT)  # … that unlocks nothing


def test_offline_pro_is_told_about_the_connection_not_the_plan(agent_project: Path, make_app) -> None:
    ent = capabilities.from_account(Account.from_dict(account_doc(PRO), cached=True))
    repl, buffer, _ = free_repl(make_app(agent_project), "Explain this repository.", "", "/quit", entitlements=ent)
    repl.run()
    out = buffer.getvalue()
    assert "HighhX platform unavailable" in out and "cannot be reached right now" in out
    assert "part of HighhX Pro" not in out


def test_edited_cache_while_online_is_overruled_by_the_platform(fake_platform: type[FakeClient]) -> None:
    _tampered_cache()
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    assert capabilities.resolve(CloudAccount()).status == "Free • Connected"


def test_edited_cache_offline_never_starts_the_runtime(
    agent_project: Path, make_app, fake_platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.agent import session

    constructed: list[int] = []
    real = session.AgentSession.__init__
    monkeypatch.setattr(
        session.AgentSession, "__init__", lambda self, *a, **k: constructed.append(1) or real(self, *a, **k)
    )
    seen: list[AgentREPL] = []
    monkeypatch.setattr(AgentREPL, "run", lambda self, *a, **k: seen.append(self) or 0)
    _tampered_cache()
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    launch.start_interactive(make_app(agent_project))
    assert seen[0].session is None and constructed == []


def test_create_session_refuses_a_cached_account(agent_project: Path, make_app, fake_platform) -> None:
    from highhx.agent.bootstrap import create_session

    _tampered_cache()
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    ui, _ = make_ui(Script())
    with pytest.raises(CloudError, match="needs the HighhX platform"):
        create_session(make_app(agent_project), CloudAccount(), ui)


def test_one_shot_agent_with_an_edited_cache_offline_fails_cleanly(cli, tmp_path: Path, fake_platform) -> None:
    _tampered_cache()
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    result = cli("agent", "fix my tests", cwd=tmp_path)
    assert result.code != 0 and "needs the HighhX platform" in result.stderr
    assert not any(path.startswith("/v1/ai") for _m, path, _b, _t in fake_platform.calls)


# --------------------------------------------------------- upgrade and outage
def test_reconnect_after_an_outage_attaches_the_agent(
    agent_project: Path, make_app, make_session, fake_platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("highhx.agent.repl.RECONNECT_SECONDS", 0.0)
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = CloudError("Cannot reach the HighhX platform")
    app = make_app(agent_project)
    cloud = CloudAccount()
    holder: dict[str, Any] = {}

    def factory() -> Any:
        s, _p, _ = make_session(agent_project, [reply("Agent is back.")], ui=holder["ui"])
        return s, Account.from_dict(account_doc(PRO)), False

    repl, buffer, _ = free_repl(
        app,
        "Explain this repository.",
        "",
        "Explain this repository.",
        "/quit",
        entitlements=capabilities.resolve(cloud),
        cloud=cloud,
        factory=factory,
    )
    holder["ui"] = repl.ui
    first_answer = repl.ui._read_line

    def platform_returns(prompt: str) -> str:
        line = first_answer(prompt)
        if line == "" and not holder.get("back"):
            holder["back"] = True
            fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
        return line

    repl.ui._read_line = platform_returns
    repl._read_line = platform_returns
    repl.run()
    out = buffer.getvalue()
    assert "Free • Platform unavailable" in out and "HighhX Pro capability" in out  # while it was down
    assert "Pro • Connected — the AI agent now handles your requests." in out and "Agent is back." in out


def test_downgrade_detaches_the_agent(agent_project: Path, make_session, fake_platform) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    script = Script("/account", "/quit")
    ui, buffer = make_ui(script)
    session, provider, _ = make_session(agent_project, [], ui=ui)
    repl = AgentREPL(session, ui, CloudAccount(), pro_account(), read_line=script)
    repl.run()
    assert repl.session is None and provider.requests == []
    assert "Free • Connected — continuing with local capabilities." in buffer.getvalue()


# ------------------------------------------------------------ nested sessions
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["-v"],
        ["--json"],
        ["agent"],
        ["agent", "fix"],
        ["-v", "agent", "fix"],
        ["-C", ".", "agent"],
        ["--config-profile", "ci", "agent", "run", "x"],
    ],
)
def test_nested_sessions_are_detected_behind_global_options(argv: list[str]) -> None:
    assert nested_session(argv)


@pytest.mark.parametrize("argv", [["status"], ["-v", "status"], ["agent", "models"], ["--help"], ["-C", ".", "test"]])
def test_normal_commands_are_not_nested(argv: list[str]) -> None:
    assert not nested_session(argv)
    assert command_words(["-C", "x", "-v", "git", "status"]) == ["git", "status"]


def test_launch_refuses_to_nest(agent_project: Path, make_app) -> None:
    from highhx.core.errors import UsageError

    app = make_app(agent_project)
    app.interactive_session = True
    with pytest.raises(UsageError, match="already in the HighhX session"):
        launch.start_interactive(app)


# ----------------------------------------------------------- deterministic rules
@pytest.mark.parametrize("text", ["open main.py", "open README.md", "open src/app/config.yaml", "open package.json"])
def test_files_are_not_websites_or_apps(text: str) -> None:
    assert parse(text) is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [("open example.com", "navigate"), ("open localhost:3000/index.html", "navigate"), ("open slack", "launch")],
)
def test_real_targets_still_work(text: str, kind: str) -> None:
    intent = parse(text)
    assert intent is not None and intent.kind == kind
    assert looks_like_file("a/b/c.py") and not looks_like_file("example.com")


def test_file_changing_intents_are_confirmed(agent_project: Path, make_app, capsys) -> None:
    (agent_project / "tidy.py").write_text("x=1\n")
    app = make_app(agent_project)
    repl, buffer, script = free_repl(app, "fix", "n", "/quit")
    repl.run()
    assert any("applies formatter and linter fixes" in p for p in script.prompts)
    assert "◉" not in buffer.getvalue()  # declined: nothing ran


# ------------------------------------------------------------ errors and permissions
def test_a_crashing_command_does_not_end_the_session(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/status", "/quit")
    assert repl._local("buggy", lambda: 1 // 0) == 1
    repl.handle("/status")
    out = buffer.getvalue()
    assert "Unexpected error: ZeroDivisionError" in out and "The session continues" in out
    assert "Free • Local" in out


def test_a_crashing_agent_turn_does_not_end_the_session(agent_project: Path, make_session) -> None:
    script = Script("boom", "/status", "/quit")
    ui, buffer = make_ui(script)
    session, _p, _ = make_session(agent_project, [RuntimeError("provider bug")], ui=ui)
    assert AgentREPL(session, ui, None, pro_account(), read_line=script).run() == 0
    out = buffer.getvalue()
    assert "Unexpected error: RuntimeError: provider bug" in out and "Turns" in out


def test_shell_commands_obey_approvals(agent_project: Path, make_app) -> None:
    victim = agent_project / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    app = make_app(agent_project, interactive=False)  # nobody to approve
    repl, buffer, _ = free_repl(app, f"!rm -rf {victim}", "/quit")
    repl.run()
    assert (victim / "keep.txt").exists()
    assert "✗ rm -rf" in buffer.getvalue()


def test_platform_features_are_plan_features() -> None:
    assert {c.value for c in capabilities.PLATFORM} == set(PLANS[PRO].features) - set(PLANS[FREE].features)
