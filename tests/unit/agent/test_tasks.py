"""Autonomous tasks: the agent works until HighhX has verified the result."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.agent.permissions import ApprovalMode
from highhx.agent.tasks import TaskRunner, TaskSpec, definition_of_done
from tests.unit.agent.conftest import RecordingUI, reply
from tests.unit.agent.test_cloud_and_cli import FakeClient, account_doc
from tests.unit.agent.test_interactive_shell import free_repl
from tests.unit.agent.test_repl import Script, make_ui, pro_account

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


@pytest.fixture(autouse=True)
def isolated_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("highhx.agent.repl._setup_readline", lambda: lambda: None)


@pytest.fixture
def buggy(agent_project: Path) -> Path:
    (agent_project / "src" / "pyapp" / "mathx.py").write_text(BROKEN)
    (agent_project / "tests" / "test_mathx.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        "from pyapp.mathx import add  # noqa: E402\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return agent_project


def fix(content: str = FIXED) -> list[Any]:
    return reply("", [("write_file", {"path": "src/pyapp/mathx.py", "content": content})])


def runner_for(session: Any, ui: RecordingUI) -> TaskRunner:
    return TaskRunner(session, ActionExecutor.for_user(session.app, ui))


# --------------------------------------------------------- definition of done
@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("Fix the login bug and make sure all tests pass", ["test"]),
        ("make the tests pass", ["test"]),
        ("run the tests, fix failures, and verify everything", ["test"]),
        ("clean up lint so the checks pass", ["check"]),
        ("refactor auth; the project builds and the tests pass", ["test", "build"]),
        ("fix the login bug", []),
        ("explain this repository", []),
    ],
)
def test_definition_of_done_is_deterministic(text: str, keys: list[str]) -> None:
    assert definition_of_done(text) == keys == definition_of_done(text)


def test_checks_without_a_command_are_reported_not_faked(buggy: Path, make_app, tmp_path: Path) -> None:
    spec = TaskSpec.build(make_app(buggy), "x", ["test", "build"])
    assert [c.action for c in spec.checks] == ["project.test", "project.build"]
    empty = TaskSpec.build(make_app(tmp_path), "x", ["test", "build"])
    assert empty.checks == [] and empty.missing == ["tests pass", "the project builds"]


# ----------------------------------------------------------------- the loop
def test_verified_on_the_first_attempt(buggy: Path, make_session) -> None:
    session, provider, ui = make_session(buggy, [fix(), reply("Fixed add().")])
    report = runner_for(session, ui).run(TaskSpec.build(session.app, "fix add", ["test"]))
    assert report.status == "verified" and report.attempts == 1 and report.ok
    assert report.checks[0].ok and report.changed_files == ["src/pyapp/mathx.py"]
    assert "Definition of done: tests pass" in provider.requests[0].messages[-1].text


def test_failed_verification_goes_back_to_the_agent(buggy: Path, make_session) -> None:
    session, provider, ui = make_session(
        buggy,
        [
            fix("def add(a, b):\n    return a * b\n"),  # attempt 1: wrong, but the agent claims success
            reply("All done!"),
            fix(),  # attempt 2 after HighhX's feedback
            reply("Now it is really fixed."),
        ],
    )
    report = runner_for(session, ui).run(TaskSpec.build(session.app, "fix add", ["test"]))
    assert report.status == "verified" and report.attempts == 2
    feedback = [m.text for m in provider.requests[2].messages if m.role == "user" and "[HighhX task]" in m.text][-1]
    assert "Goal: fix add" in feedback and "Verification failed after attempt 1 of 3" in feedback
    assert "assert 6 == 5" in feedback  # the real test output reaches the agent


def test_unverified_after_the_attempts_run_out(buggy: Path, make_session) -> None:
    session, _provider, ui = make_session(buggy, [reply("done"), reply("done again")])
    spec = TaskSpec.build(session.app, "fix add", ["test"], max_attempts=2)
    report = runner_for(session, ui).run(spec)
    assert report.status == "unverified" and report.attempts == 2 and not report.ok
    assert not report.checks[0].ok
    assert (buggy / "src" / "pyapp" / "mathx.py").read_text() == BROKEN


def test_no_checks_means_one_turn(buggy: Path, make_session) -> None:
    session, provider, ui = make_session(buggy, [reply("Explained.")])
    report = runner_for(session, ui).run(TaskSpec.build(session.app, "explain", []))
    assert report.status == "done" and report.attempts == 1 and report.checks == []
    assert len(provider.requests) == 1


def test_the_task_is_recorded(buggy: Path, make_session) -> None:
    session, _provider, ui = make_session(buggy, [fix(), reply("ok")])
    events: list[str] = []
    session.app.ctx.events.subscribe("*", lambda e: events.append(e.name))
    runner_for(session, ui).run(TaskSpec.build(session.app, "fix add", ["test"]))
    assert [e for e in events if e.startswith("task.")] == [
        "task.started",
        "task.attempt",
        "task.verified",
        "task.completed",
    ]
    record = session.app.history.list(kind="task", limit=1)[0]
    assert record.status == "success" and record.metadata["status"] == "verified"


def test_cancellation_stops_the_task(buggy: Path, make_session) -> None:
    from highhx.execution.cancellation import CancellationToken

    token = CancellationToken()
    token.cancel("user")
    session, _provider, ui = make_session(buggy, [reply("never")])
    report = runner_for(session, ui).run(TaskSpec.build(session.app, "fix add", ["test"]), cancel=token)
    assert report.status == "cancelled" and report.checks == []


# ---------------------------------------------------------------- the session
def test_a_pro_request_with_a_definition_of_done_runs_as_a_task(buggy: Path, make_session) -> None:
    script = Script("Fix add and make sure all tests pass", "/status", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(buggy, [fix(), reply("Fixed.")], ui=ui, mode=ApprovalMode.AUTO_EDIT)
    from highhx.agent.repl import AgentREPL

    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "Done when: tests pass" in out and "Verified by HighhX: tests pass" in out
    assert "Task verified" in out and "Last task" in out and "verified after 1 attempt" in out


def test_task_command_and_retry(buggy: Path, make_session) -> None:
    script = Script("/task fix add --verify test --attempts 1", "/retry", "/task", "/task x --verify nope", "/quit")
    ui, buffer = make_ui(script)
    session, _provider, _ = make_session(
        buggy, [reply("done"), fix(), reply("fixed")], ui=ui, mode=ApprovalMode.AUTO_EDIT
    )
    from highhx.agent.repl import AgentREPL

    AgentREPL(session, ui, None, pro_account(), read_line=script).run()
    out = buffer.getvalue()
    assert "Task not verified" in out and "/retry to give the agent more attempts" in out
    assert "Task verified" in out  # /retry ran the task again
    assert "Usage: /task <goal>" in out and "Unknown check(s): nope" in out


def test_task_is_pro_only_on_free(agent_project: Path, make_app) -> None:
    repl, buffer, _ = free_repl(make_app(agent_project), "/task fix the bug", "", "/quit")
    repl.run()
    assert "/task is part of the AI agent, which requires HighhX Pro." in buffer.getvalue()


# ----------------------------------------------------------------- one-shot
def test_one_shot_verify(buggy: Path, cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.agent import bootstrap
    from highhx.cloud import credentials
    from tests.unit.agent.conftest import ScriptedProvider

    FakeClient.calls, FakeClient.routes = [], {("GET", "/v1/me"): account_doc("pro")}
    monkeypatch.setattr("highhx.cloud.account.PlatformClient", FakeClient)
    monkeypatch.delenv("HIGHHX_TOKEN", raising=False)
    credentials.save(credentials.Credentials(token="hhx_t"))
    monkeypatch.setattr(bootstrap, "build_provider", lambda *a, **k: ScriptedProvider([fix(), reply("fixed")]))
    result = cli("agent", "--yes", "--mode", "auto-edit", "--verify", "test", "--json", "fix add", cwd=buggy)
    data = json.loads(result.stdout)
    assert result.code == 0 and data["status"] == "verified" and data["checks"][0]["ok"]
    bad = cli("agent", "--verify", "bogus", "fix add", cwd=buggy)
    assert bad.code == 2 and "Unknown check" in bad.stderr


PY = sys.executable
