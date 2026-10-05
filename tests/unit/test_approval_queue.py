"""The approval framework: approve, reject, modify (re-planned and asked again), defer, timeout,
typed confirmation for critical actions — answered from a queue (the web console), through the
real gate and executor."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.approvals.queue import ApprovalQueue, QueuePrompter
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


def executor(app: Any, queue: ApprovalQueue, actor: Actor = Actor.AGENT) -> ActionExecutor:
    gate = ActionGate(
        app.engine, QueuePrompter(queue), source="web", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    return ActionExecutor(app, gate, actor=actor)


def in_background(fn: Any) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: out.setdefault("result", fn()), daemon=True)
    thread.start()
    return thread, out


def next_pending(queue: ApprovalQueue, seen: int = 0, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = queue.pending()
        if len(queue.all()) > seen and pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("no approval was requested")


def test_approve_and_reject(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    queue = ApprovalQueue()
    ex = executor(app, queue)
    thread, out = in_background(lambda: ex.run("filesystem.write", {"path": "a.txt", "content": "x\n"}))
    item = next_pending(queue)
    assert item.kind in ("permission", "confirm") and "a.txt" in item.action
    queue.decide(item.id, "approve", by="tester")
    thread.join(5)
    assert out["result"].ok and (agent_project / "a.txt").read_text() == "x\n"
    thread, out = in_background(lambda: ex.run("filesystem.write", {"path": "b.txt", "content": "y\n"}))
    queue.decide(next_pending(queue, 1).id, "reject")
    thread.join(5)
    assert out["result"].status == "denied" and not (agent_project / "b.txt").exists()


def test_modify_replans_and_asks_again(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    queue = ApprovalQueue()
    ex = executor(app, queue, actor=Actor.USER)
    # deleting a file is high risk: a sensitive confirmation, which can be modified
    (agent_project / "old.txt").write_text("1")
    (agent_project / "keep.txt").write_text("2")
    thread, out = in_background(lambda: ex.run("filesystem.delete", {"path": "keep.txt"}))
    first = next_pending(queue)
    assert first.kind == "confirm" and "keep.txt" in first.action
    with pytest.raises(ValueError, match="modify needs"):
        queue.decide(first.id, "modify")  # no new inputs: refused, still pending
    queue.decide(first.id, "modify", inputs={"path": "old.txt"})
    second = next_pending(queue, 1)  # the modified action is asked about again, never assumed
    assert "old.txt" in second.action
    queue.decide(second.id, "approve")
    thread.join(5)
    assert out["result"].ok and not (agent_project / "old.txt").exists() and (agent_project / "keep.txt").exists()


def test_defer_extends_the_deadline_and_silence_rejects(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    queue = ApprovalQueue(timeout=0.4)
    ex = executor(app, queue)
    started = time.monotonic()
    thread, out = in_background(lambda: ex.run("filesystem.write", {"path": "c.txt", "content": "z\n"}))
    item = next_pending(queue)
    time.sleep(0.3)
    queue.decide(item.id, "defer")  # the deadline moves to 0.4 s after this
    thread.join(5)
    assert out["result"].status == "denied" and not (agent_project / "c.txt").exists()
    assert item.status == "expired" and item.defers == 1 and time.monotonic() - started >= 0.7


def test_critical_actions_need_the_typed_word(agent_project: Path, make_app) -> None:  # noqa: F811
    app = make_app(agent_project)
    queue = ApprovalQueue()
    ex = executor(app, queue, actor=Actor.USER)
    (agent_project / "build").mkdir()
    thread, out = in_background(lambda: ex.run("shell.run", {"command": f"rm -rf {agent_project / 'build'}"}))
    item = next_pending(queue)
    assert item.confirm_word == "approve"
    with pytest.raises(ValueError, match="type 'approve'"):
        queue.decide(item.id, "approve")
    queue.decide(item.id, "approve", typed="approve")
    thread.join(10)
    assert out["result"].ok and not (agent_project / "build").exists()
    with pytest.raises(ValueError, match="already approved"):
        queue.decide(item.id, "reject")
    ex.close()
    app.close()


def test_cancelling_the_task_withdraws_its_waiting_approval() -> None:
    # Regression: a task cancelled while it waited for an answer kept waiting until the deadline
    # (five minutes); the approval could even be approved after the cancel.
    from highhx.execution.cancellation import CancellationToken

    token = CancellationToken()
    queue = ApprovalQueue()
    prompter = QueuePrompter(queue, cancel=lambda: token)
    thread, out = in_background(lambda: prompter.ask_permission("write notes.txt", ()))
    item = next_pending(queue)
    started = time.monotonic()
    token.cancel("cancelled from the web console")
    thread.join(5)
    assert out["result"] == "no" and time.monotonic() - started < 2
    assert item.status == "cancelled" and queue.pending() == []
    with pytest.raises(ValueError, match="already cancelled"):
        queue.decide(item.id, "approve")


def test_the_same_answer_twice_is_one_answer() -> None:
    queue = ApprovalQueue()
    thread, _ = in_background(lambda: QueuePrompter(queue).ask_permission("write a.txt", ()))
    item = next_pending(queue)
    queue.decide(item.id, "approve", by="web console")
    assert queue.decide(item.id, "approve", by="web console").status == "approved"  # a double click: no error
    with pytest.raises(ValueError, match="already approved"):
        queue.decide(item.id, "reject")  # a different answer afterwards is refused
    thread.join(5)
