"""The debugger view of a run: every event with time, component, action, latency, result and
error; filtered, searched and exported (CLI and web console)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
from highhx.observability.tasktrace import TaskTraceRecorder, TraceStore
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401


def test_a_run_becomes_a_filterable_timeline(cli, agent_project: Path, make_app) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    app = make_app(agent_project)
    store = TraceStore.for_app(app)
    recorder = TaskTraceRecorder(app.ctx.events, store, redactor=app.redactor)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    steps = [
        {"action": "filesystem.write", "parameters": {"path": "ok.txt", "content": "x\n"}},
        {"action": "filesystem.read", "parameters": {"path": "missing.txt"}},
    ]
    result = AgentLoop(executor, ScriptedPlanner(steps), traces=store, sleep=lambda _s: None).run(
        AgentTask("two steps", surface="none", max_failures=0)
    )
    recorder.flush()
    trace = store.load(result.trajectory.trace_id)
    rows = trace.timeline()
    assert rows[0]["t"] == 0.0 and all(rows[i]["t"] <= rows[i + 1]["t"] for i in range(len(rows) - 1))
    actions = trace.timeline(component="action")
    assert {r["action"] for r in actions} >= {"filesystem.write", "filesystem.read"}
    completed = next(r for r in actions if r["event"] == "action.completed")
    assert isinstance(completed["latency"], float)
    failures = trace.timeline(failures=True)
    assert (
        failures
        and all(r["failed"] for r in failures)
        and any("missing.txt" in r["error"] or r["action"] == "filesystem.read" for r in failures)
    )
    found = trace.timeline(search="ok.txt")
    assert found and len(found) < len(rows)  # narrowed to the events that mention it
    assert trace.timeline(search="no-such-text-anywhere") == []
    app.close()
    out = cli(
        "trace", "timeline", result.trajectory.trace_id, "--component", "action", "--export", "csv", cwd=agent_project
    )
    assert out.code == 0
    parsed = list(csv.DictReader(io.StringIO(out.stdout)))
    assert parsed and {"t", "event", "action", "latency", "result", "error"} <= set(parsed[0])
    assert all(row["canonical"].startswith("action.") for row in parsed)
