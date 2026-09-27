"""The interactive session on the action engine, and the structured event log."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from highhx.actions import events as ev
from highhx.actions.events import EventLog, read_events
from highhx.core.events import EventBus
from highhx.security.secrets import Redactor
from tests.unit.agent.test_interactive_shell import free_repl

PY = sys.executable


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


# --------------------------------------------------------------- event log
def test_event_log_is_structured_filtered_and_redacted(tmp_path: Path) -> None:
    redactor = Redactor()
    redactor.add(["sk-live-supersecret-123456"])
    bus = EventBus()
    log = EventLog(redactor, directory=tmp_path, session_id="s-1")
    detach = log.attach(bus)
    bus.emit(ev.ACTION_COMPLETED, action="shell.run", summary="token sk-live-supersecret-123456 used")
    bus.emit("command.output", line="noise")  # not an execution event: not logged
    bus.emit(ev.INTENT_RESOLVED, text="run the tests", rule="test", actions=["project.test"])
    detach()
    bus.emit(ev.ACTION_FAILED, action="after-detach")
    raw = "".join(p.read_text() for p in tmp_path.glob("*.jsonl"))
    assert "supersecret" not in raw and "[REDACTED]" in raw
    records = read_events(directory=tmp_path)
    assert [r["event"] for r in records] == [ev.ACTION_COMPLETED, ev.INTENT_RESOLVED]
    assert all(r["session"] == "s-1" for r in records)
    assert read_events(directory=tmp_path, prefix="intent.")[0]["actions"] == ["project.test"]
    assert read_events(directory=tmp_path, session="other") == []


def test_event_names_cover_the_lifecycle() -> None:
    required = {
        "session.started",
        "intent.resolved",
        "action.planned",
        "approval.requested",
        "approval.granted",
        "action.started",
        "action.completed",
        "action.failed",
        "workflow.started",
        "workflow.completed",
        "agent.started",
        "agent.turn",
        "agent.tool_call",
        "agent.completed",
    }
    assert required <= set(ev.EVENT_NAMES)


def test_events_command(cli, tmp_path: Path) -> None:
    EventLog(Redactor(), session_id="s-9").write(
        __import__("highhx.core.events", fromlist=["Event"]).Event(ev.ACTION_COMPLETED, {"action": "git.status"})
    )
    data = cli("events", "--json", "--session", "s-9", cwd=tmp_path).json()
    assert data["events"][0]["action"] == "git.status"


# ------------------------------------------------------------ session flows
def test_plan_then_approve_runs_exactly_the_preview(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, script = free_repl(app, "/plan install dependencies then run the tests", "/approve", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "package.install" in out and "project.test" in out and "/approve runs exactly this plan" in out
    assert not any("Approve?" in p for p in script.prompts)  # the medium-risk install was approved by /approve
    assert "◉ package.install" in out and "◉ project.test" in out


def test_deny_discards_and_nothing_runs(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/plan format the code", "/deny", "/approve", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "Plan discarded; nothing ran." in out and "No plan is waiting" in out and "◉" not in out


def test_plan_for_open_ended_requests_does_not_invent_steps(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/plan fix the failing tests", "/approve", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "it needs the AI agent (HighhX Pro)" in out and "No plan is waiting" in out


def test_blocked_plans_cannot_be_approved(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, _buffer, _ = free_repl(app, "/quit")
    repl.run()
    repl.cmd_run('shell.run command="rm -rf /"')
    out = _buffer.getvalue()
    assert "blocked by policy" in out and not repl.pending


def test_run_retry_and_changes_undo(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(
        app,
        "/run filesystem.read path=missing.txt",
        "/retry",
        "/run filesystem.write path=notes.txt content=hello",
        "y",
        "/changes",
        "/undo",
        "/retry",
        "/quit",
    )
    repl.run()
    out = buffer.getvalue()
    assert out.count("missing.txt does not exist") >= 2  # the retry ran it again
    assert "created  notes.txt" in out
    assert "Restored 1 file(s): notes.txt" in out and not (agent_project / "notes.txt").exists()
    assert "Nothing to retry — the last request succeeded" in out


def test_run_parses_inputs(agent_project: Path, make_app) -> None:
    from highhx.agent.repl import parse_inputs

    assert parse_inputs("""path=a.txt n=3 flag=true tags='["a","b"]'""") == {
        "path": "a.txt",
        "n": 3,
        "flag": True,
        "tags": ["a", "b"],
    }
    assert parse_inputs('{"path": "a"}') == {"path": "a"}
    with pytest.raises(ValueError):
        parse_inputs("oops")


def test_resume_and_cancel_in_the_session(agent_project: Path, make_app) -> None:
    (agent_project / ".highhx" / "workflows").mkdir(parents=True, exist_ok=True)
    (agent_project / ".highhx" / "workflows" / "gate.yaml").write_text(
        f"name: gate\nsteps:\n  - id: g\n    run: {PY} -c \"import os,sys; sys.exit(0 if os.path.exists('ok') else 1)\"\n"
    )
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/workflow run gate", "/cancel", "/quit")
    repl.run()
    (agent_project / "ok").write_text("")
    repl.cmd_resume("")
    out = buffer.getvalue()
    assert "No workflow is running" in out
    assert "✓ highhx workflow resume" in out


def test_tools_by_category_and_status(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "/tools git", "/tools nope", "/tools", "/quit")
    repl.run()
    out = buffer.getvalue()
    assert "git.push" in out and "high" in out
    assert "No action category 'nope'" in out
    assert "actions" in out and "filesystem" in out


def test_session_events_are_written_with_the_session_id(agent_project: Path, make_app) -> None:
    app = make_app(agent_project)
    repl, _buffer, _ = free_repl(app, "run the tests", "/history", "/quit")
    log = EventLog(app.redactor, session_id=repl.session_id)
    detach = log.attach(app.ctx.events)
    try:
        repl.run()
    finally:
        detach()
    names = [r["event"] for r in read_events(session=repl.session_id)]
    assert "intent.resolved" in names and "action.completed" in names
    assert "project.test" in _buffer.getvalue()


def test_shell_commands_are_actions(agent_project: Path, make_app, capsys) -> None:
    app = make_app(agent_project)
    repl, buffer, _ = free_repl(app, "!echo hello-action", "/quit")
    repl.run()
    assert "◉ echo hello-action  shell.run · low" in buffer.getvalue()
    assert "hello-action" in capsys.readouterr().out
    history = app.history.list(kind="action", limit=1)
    assert history[0].name == "shell.run"


def test_actions_cli(agent_project: Path, cli) -> None:
    listed = cli("actions", "list", "git", "--json", cwd=agent_project).json()["actions"]
    assert {"git.status", "git.push"} <= {a["name"] for a in listed}
    shown = cli("actions", "show", "filesystem.write", "--json", cwd=agent_project).json()
    assert shown["risk"] == "medium" and shown["compensation"] == "yes" and "path" in shown["inputs"]["properties"]
    planned = cli("actions", "plan", "shell.run", "command=rm -rf /", "--json", cwd=agent_project).json()
    assert planned["blocked"] is True
    ran = cli("actions", "run", "filesystem.read", "path=pyproject.toml", "--json", cwd=agent_project)
    assert ran.code == 0 and json.loads(ran.stdout)["output"]["path"] == "pyproject.toml"
    denied = cli("actions", "run", "filesystem.write", "path=a.txt", "content=x", "--json", cwd=agent_project)
    assert denied.code == 6 and not (agent_project / "a.txt").exists()
    assert cli("actions", "show", "nope.nope", cwd=agent_project).code == 2
    assert cli("actions", "run", "shell.run", "command=rm -rf /", "--yes", cwd=agent_project).code == 7  # blocked
