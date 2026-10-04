"""A secret typed during a computer-use task never appears anywhere HighhX writes or shows:
the event log, task traces, trajectories and checkpoints, audit rows, history and logs, the live
dashboard, replays — nor a credential used by api.request."""

from __future__ import annotations

import http.server
import io
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from highhx.actions.events import EventLog
from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
from highhx.benchmarks.environments.web import BASE, FakeWebApp
from highhx.computer.session import ComputerSession
from highhx.observability.stream import EventRecorder
from highhx.observability.tasktrace import TraceStore
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore
from highhx.ui.live import DashboardState, render
from tests.unit.agent.conftest import RecordingUI

SECRET = "pa55-Word-9f3c71"
TOKEN = "tok-4e1b9d0c77aa"


def everything_written(root: Path) -> str:
    """The text of every file under ``root`` (SQLite databases dumped row by row)."""
    parts: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in (".db", ".sqlite") or path.name.endswith(".db"):
            connection = sqlite3.connect(path)
            try:
                for (table,) in connection.execute("select name from sqlite_master where type='table'"):
                    parts += [repr(row) for row in connection.execute(f"select * from {table}")]
            finally:
                connection.close()
        else:
            parts.append(path.read_bytes().decode("utf-8", "replace"))
    return "\n".join(parts)


def test_a_typed_password_appears_nowhere(agent_project: Path, make_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = FakeWebApp(url=f"{BASE}/form")
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: web))
    app = make_app(agent_project)
    log = EventLog(app.redactor, directory=tmp_path / "events")
    log.attach(app.ctx.events)
    recorder = EventRecorder.attach(app.ctx.events, redactor=app.redactor)
    state = DashboardState()
    recorder.listen(state.apply)
    gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.USER, sleep=lambda _s: None)
    store = TrajectoryStore(tmp_path / "trajectories", redactor=app.redactor)
    traces = TraceStore(tmp_path / "traces")
    script = [
        {"action": "type", "target": {"label": "Email", "role": "textbox"}, "parameters": {"text": "me@example.com"}},
        {"action": "type", "target": {"label": "Password", "role": "textbox"}, "parameters": {"text": SECRET}},
        {"action": "click", "target": {"label": "Submit", "role": "button"}},
    ]
    result = AgentLoop(executor, ScriptedPlanner(script), store=store, traces=traces, sleep=lambda _s: None).run(
        AgentTask("sign up", surface="browser", success={"text": "Thank you"})
    )
    assert result.ok and web.state["password"] == SECRET, result.trajectory.describe()  # it was really typed
    app.close()
    written = everything_written(tmp_path) + everything_written(agent_project / ".highhx")
    from highhx.utils.paths import user_data_dir

    written += everything_written(user_data_dir())
    assert SECRET not in written
    console = Console(file=io.StringIO(), width=200, record=True, color_system=None)
    console.print(render(state))
    assert SECRET not in console.export_text()
    assert all(SECRET not in json.dumps(r.payload) for r in recorder.snapshot())


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def test_a_credential_used_by_a_request_appears_nowhere(agent_project: Path, make_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SHOP_TOKEN", TOKEN)
    try:
        app = make_app(agent_project)
        EventLog(app.redactor, directory=tmp_path / "events").attach(app.ctx.events)
        gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
        executor = ActionExecutor(app, gate, actor=Actor.USER)
        result = executor.run("api.request", {"url": f"http://127.0.0.1:{server.server_address[1]}/me", "headers_from_env": {"Authorization": "SHOP_TOKEN"}})
        assert result.ok and result.output["json"]["auth"] == TOKEN  # the server received it …
        app.close()
    finally:
        server.shutdown()
        server.server_close()
    from highhx.utils.paths import user_data_dir

    written = everything_written(tmp_path) + everything_written(user_data_dir()) + everything_written(agent_project / ".highhx")
    assert TOKEN not in written  # … and nothing HighhX wrote keeps it


def test_query_string_values_never_reach_audit_or_events(agent_project: Path, make_app, tmp_path: Path) -> None:
    app = make_app(agent_project)
    EventLog(app.redactor, directory=tmp_path / "events").attach(app.ctx.events)
    gate = ActionGate(app.engine, RecordingUI(default_action_answer=False), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    # an external read is asked first; declining it still writes an audit row and events
    result = executor.run("api.request", {"url": "https://api.example.com/v1/me?api_key=qk-77aa-secret"})
    assert result.status == "denied"
    app.close()
    from highhx.utils.paths import user_data_dir

    written = everything_written(tmp_path) + everything_written(user_data_dir()) + everything_written(agent_project / ".highhx")
    assert "qk-77aa-secret" not in written and "api_key=…" in written


def test_query_values_of_opened_and_extracted_urls_never_reach_audit_or_events(agent_project: Path, make_app, tmp_path: Path) -> None:
    """browser.open and browser.extract {url}: the classifier sees the whole URL (sensitive-URL rules),
    but audit rows and events keep parameter names only."""
    app = make_app(agent_project)
    EventLog(app.redactor, directory=tmp_path / "events").attach(app.ctx.events)
    gate = ActionGate(app.engine, RecordingUI(default_action_answer=False, default_permission="no"), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.AGENT)  # the agent's actions are asked; declined: nothing runs
    for action in ("browser.open", "browser.extract"):
        result = executor.run(action, {"url": "https://shop.example.com/reset?token=rt-91zz-secret&step=2"})
        assert result.status == "denied", (action, result.status, result.error)
    app.close()
    from highhx.utils.paths import user_data_dir

    written = everything_written(tmp_path) + everything_written(user_data_dir()) + everything_written(agent_project / ".highhx")
    assert "rt-91zz-secret" not in written
    assert "reset?token=…&step=…" in written  # the audit row keeps parameter names only
    assert "step=2" not in written  # not only secret-looking values: every query value


def test_a_failed_navigation_does_not_carry_its_query_values_into_records(agent_project: Path, make_app, tmp_path: Path, monkeypatch) -> None:
    """The handler's error quotes the URL ("Could not open …?token=…"): history, audit and events
    keep parameter names only."""
    import dataclasses

    from highhx.actions.spec import ActionResult

    url = "https://shop.example.com/reset?token=rt-55qq-secret&q=private-words"

    def failing(ctx, inputs):  # type: ignore[no-untyped-def]
        return ActionResult(False, error=f"Could not open {inputs['url']}: net::ERR_NAME_NOT_RESOLVED", summary=f"open {inputs['url']}")

    app = make_app(agent_project)
    EventLog(app.redactor, directory=tmp_path / "events").attach(app.ctx.events)
    gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    real = executor.catalog.get("browser.open")
    monkeypatch.setattr(executor.catalog, "get", lambda name: dataclasses.replace(real, handler=failing) if name == "browser.open" else type(executor.catalog).get(executor.catalog, name))
    result = executor.run("browser.open", {"url": url})
    assert not result.ok and "token=…&q=…" in result.error
    app.close()
    from highhx.utils.paths import user_data_dir

    written = everything_written(tmp_path) + everything_written(user_data_dir()) + everything_written(agent_project / ".highhx")
    assert "rt-55qq-secret" not in written and "private-words" not in written
