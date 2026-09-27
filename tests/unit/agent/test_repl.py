"""The interactive experience: banner, turns, slash commands and terminal rendering."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from highhx.agent.messages import ToolCall
from highhx.agent.planner import Plan, PlanStep
from highhx.agent.repl import SLASH_COMMANDS, AgentREPL
from highhx.agent.tools.base import ToolResult
from highhx.agent.ui import TerminalUI, format_tokens
from highhx.cloud.account import Account
from highhx.cloud.plans import PLANS, PRO
from highhx.ui.terminal import UNICODE_SYMBOLS
from tests.unit.agent.conftest import reply


class Script:
    """Feeds lines to the REPL; raises EOFError when exhausted (Ctrl+D)."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


def make_ui(read: Script, *, terminal: bool = False) -> tuple[TerminalUI, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=terminal, width=120, color_system=None, highlight=False)
    return TerminalUI(console, UNICODE_SYMBOLS, interactive=True, read_line=read), buffer


def pro_account() -> Account:
    plan = PLANS[PRO]
    return Account("u1", "dev@example.com", "Dev", plan, plan.features)


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


def run_repl(agent_project: Path, make_session, steps, *lines: str, terminal: bool = False):
    script = Script(*lines)
    ui, buffer = make_ui(script, terminal=terminal)
    session, provider, _ = make_session(agent_project, steps, ui=ui)
    repl = AgentREPL(session, ui, cloud=None, account=pro_account(), read_line=script)  # type: ignore[arg-type]
    code = repl.run()
    return code, buffer.getvalue(), session, provider


def test_banner_turn_and_footer(agent_project: Path, make_session) -> None:
    code, out, session, _ = run_repl(
        agent_project,
        make_session,
        [reply("", [("list_files", {"glob": "*.py"})]), reply("There are **two** Python files.")],
        "how many python files?",
        "/quit",
    )
    assert code == 0
    assert "HighhX v" in out and "AI Developer Agent · Pro" in out
    assert "Project" in out and "pyapp" in out and "Python" in out and "Connected" in out
    assert "What would you like me to do?" in out
    assert "✓ List *.py — 3 file(s)" in out
    assert "There are **two** Python files." in out
    assert "1 step" in out and "tokens" in out
    assert "Session saved" in out
    assert session.record is not None and session.record.status == "closed"


def test_slash_commands(agent_project: Path, make_session) -> None:
    (agent_project / "notes.txt").write_text("before\n")
    code, out, session, _ = run_repl(
        agent_project,
        make_session,
        [
            reply("", [("propose_plan", {"goal": "Tidy up", "steps": ["Edit notes", "Verify"]})]),
            reply("", [("write_file", {"path": "notes.txt", "content": "after\n"})]),
            reply("", [("update_plan", {"step": 1, "status": "done", "note": "rewritten"})]),
            reply("Done."),
        ],
        "/help",
        "tidy the notes",
        "y",  # approve the plan
        "y",  # approve the file change
        "/plan",
        "/changes",
        "/undo",
        "/status",
        "/context",
        "/mode auto-edit",
        "/mode nonsense",
        "/memory",
        "/history",
        "/nope",
        "/clear",
        "/exit",
    )
    assert code == 0
    for command in SLASH_COMMANDS:
        assert command.usage in out
    assert "Plan" in out and "Tidy up" in out and "1. " in out
    assert "Action requires approval" in out and "Rewrite notes.txt (+1 -1)" in out
    assert "+after" in out and "-before" in out  # diff shown before approval
    assert "1 done · 0 failed · 1 remaining" in out
    assert "modified" in out and "notes.txt" in out
    assert "Restored 1 file(s): notes.txt" in out
    assert (agent_project / "notes.txt").read_text() == "before\n"
    assert "dev@example.com" in out and "HighhX Pro" in out
    assert "Files indexed" in out and "Tools" in out
    assert "Approval mode is now auto-edit" in out and "Choose one of" in out
    assert "No project memory yet" in out
    assert "Unknown command /nope" in out
    assert "Conversation cleared" in out
    assert session.messages == []


def test_model_command_lists_and_switches(agent_project: Path, make_session, monkeypatch) -> None:
    from highhx.cloud import credentials

    credentials.save(credentials.Credentials(token="hhx_t"))
    from highhx.cloud.account import CloudAccount

    script = Script("/model", "/model openai", "/model skynet-9", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(agent_project, [], ui=ui)
    AgentREPL(session, ui, cloud=CloudAccount(), account=pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "via the HighhX gateway" in out and "claude-opus-5" in out
    assert "Now using OpenAI · gpt-5" in out
    assert session.provider.name == "highhx" and session.settings.provider == "openai"
    assert "not a known model" in out


def test_ctrl_c_twice_exits(agent_project: Path, make_session) -> None:
    class Interrupting(Script):
        def __call__(self, prompt: str) -> str:
            raise KeyboardInterrupt

    script = Interrupting()
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(agent_project, [], ui=ui)
    assert AgentREPL(session, ui, cloud=None, account=pro_account(), read_line=script).run() == 0  # type: ignore[arg-type]
    assert "Press Ctrl+C again" in buffer.getvalue()


def test_terminal_rendering_paths() -> None:
    script = Script("n", "fewer steps", "a", "yes", "prod")
    ui, buffer = make_ui(script, terminal=True)
    ui.assistant_started()
    ui.assistant_text("# Title\n")
    ui.assistant_text("Some *markdown*.")
    ui.assistant_finished()
    call = ToolCall("c1", "run_tests", {})
    ui.tool_started(None, call, "Run tests")
    ui.tool_output("collecting ...")
    ui.tool_finished(None, call, ToolResult("boom", ok=False, summary="3 tests failing"), 2.5)
    plan = Plan("Release", [PlanStep("Test"), PlanStep("Deploy")])
    assert ui.present_plan(plan) == (False, "fewer steps")
    plan.approved = True
    plan.steps[0].status = "done"
    ui.plan_updated(plan, 0)
    assert ui.ask_permission("Edit a.py", ["--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"]) == "always"
    assert ui.confirm("Push?") is True
    assert ui.confirm_typed("Deploy to production", "prod") is True
    ui.notice("warn", "careful")
    out = buffer.getvalue()
    assert "Title" in out and "markdown" in out
    assert "✗" in out and "3 tests failing" in out
    assert "Release" in out and "✓" in out
    assert "Action requires approval" in out and "High-risk action" in out
    assert "careful" in out


def test_format_tokens() -> None:
    assert [format_tokens(n) for n in (12, 12_345, 3_400_000)] == ["12", "12.3k", "3.4M"]
