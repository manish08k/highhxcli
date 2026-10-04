"""Network verification in real Chrome. Opt-in: HIGHHX_TEST_BROWSER=1.

A click makes the page POST to an API; the agent loop verifies the step from the network evidence
DevTools reported (not from guesses), and the evidence is in the trajectory without the token
that was in the query string."""

from __future__ import annotations

import http.server
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner
from highhx.computer.browser import find_browser
from highhx.computer.session import ComputerSession
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore
from tests.unit.agent.conftest import RecordingUI

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)
PAGE = (
    "<!doctype html><title>Orders</title><p id=s></p>"
    "<button type=button onclick=\"fetch('/api/orders?token=tok-SECRET-1',{method:'POST',body:'{}'})"
    ".then(r=>{s.textContent='status '+r.status})\">Place</button>"
    "<button type=button onclick=\"setTimeout(()=>fetch('/api/slow?key=k-SECRET-2',{method:'POST',body:'{}'})"
    ".then(r=>{s.textContent='slow '+r.status}),400)\">Later</button>"
)


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/api/slow"):
            time.sleep(0.3)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": 1}).encode())

    def log_message(self, *args: Any) -> None:
        pass


def test_a_step_is_verified_by_the_request_it_caused(agent_project: Path, make_app, tmp_path: Path) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state", headless=True)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    events: list[Any] = []
    app.ctx.events.subscribe("network.observed", events.append)
    store = TrajectoryStore(tmp_path / "trajectories")
    try:
        script = [
            {"action": "open", "parameters": {"url": f"{base}/"}},
            {
                "action": "click",
                "target": {"label": "Place", "role": "button"},
                "verify": {"network": {"url_contains": "/api/orders", "method": "POST", "status": 201}},
            },
        ]
        result = AgentLoop(executor, ScriptedPlanner(script), store=store, sleep=lambda _s: None).run(
            AgentTask("place the order", surface="browser")
        )
        assert result.ok, result.trajectory.describe()
        step = store.load(result.trajectory.id).steps[-1]
        assert step.verification["report"]["verdict"] == "satisfied"
        evidence = step.result["network"]
        assert any(
            e["method"] == "POST" and e["url"].endswith("/api/orders?token=…") and e["status"] == 201 for e in evidence
        )
        assert events and "tok-SECRET-1" not in json.dumps([e.data for e in events])
        assert "tok-SECRET-1" not in store.path(result.trajectory.id).read_text()
    finally:
        if session._browser is not None:
            session._browser.stop()
        session.close()
        server.shutdown()
        server.server_close()


def test_waiting_for_a_request_and_for_the_network_to_be_idle(agent_project: Path, make_app, tmp_path: Path) -> None:
    """browser.wait on network evidence in real Chrome: the click sends a POST 0.4s later that
    takes 0.3s; the wait sees it (with its real duration), then the network settles."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state", headless=True)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    try:
        assert executor.run("browser.open", {"url": f"{base}/"}).ok
        assert executor.run("browser.wait", {"network_idle": True, "timeout": 10}).ok
        assert executor.run("browser.click", {"target": 'button:"Later"'}).ok
        waited = executor.run(
            "browser.wait", {"request": {"url_contains": "/api/slow", "method": "POST", "status": 201}, "timeout": 10}
        )
        assert waited.ok, waited.error
        (found,) = waited.output["network"]
        assert found["url"].endswith("/api/slow?key=…") and found["ms"] >= 250
        assert "k-SECRET-2" not in json.dumps(waited.output)
        assert executor.run("browser.wait", {"network_idle": True, "idle_ms": 300, "timeout": 10}).ok
        started = time.monotonic()
        missing = executor.run("browser.wait", {"request": {"url_contains": "/api/never"}, "timeout": 1})
        assert not missing.ok and "timed out" in missing.error and time.monotonic() - started < 5
    finally:
        if session._browser is not None:
            session._browser.stop()
        session.close()
        server.shutdown()
        server.server_close()
