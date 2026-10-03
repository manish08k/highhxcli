"""Browser workflow recording (events → semantic steps, secrets → variables), the DevTools
recorder, storage, and replay with self-healing through the agent loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.computer.recorder import (
    BINDING,
    BrowserRecorder,
    BrowserWorkflow,
    WorkflowStore,
    record_script,
    replay,
    steps_from_events,
    variables_from,
)
from highhx.computer.session import ComputerSession
from highhx.core.errors import NotFoundError, UsageError
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore
from tests.unit.agent.conftest import RecordingUI
from tests.unit.agent_loop.fake_web import BASE, FakeWebApp

URL = f"{BASE}/"


def ev(kind: str, role: str = "", name: str = "", at: float = 0, url: str = URL, **kw: Any) -> dict[str, Any]:
    attributes = kw.pop("attributes", {})
    secret = kw.pop("secret", False)
    element = {"role": role, "name": name, "secret": secret, "attributes": attributes, "bounds": [10, 20, 100, 30]}
    return {"kind": kind, "url": url, "title": "Shop", "viewport": [1280, 800], "element": element, "at": at, **kw}


def test_events_become_semantic_steps_with_inferred_checks() -> None:
    events = [
        {"kind": "navigate", "url": URL, "at": 0},
        ev("click", "link", "Invoices", 1000, attributes={"href": "/invoices", "tag": "a", "class": "nav css-9a8b7c"}),
        {"kind": "navigate", "url": f"{BASE}/invoices", "at": 1500},
        ev("click", "button", "Export", 3000, url=f"{BASE}/invoices", attributes={"testid": "export-invoices", "tag": "button"}),
    ]
    steps, variables = steps_from_events(events)
    assert variables == [] and [s.action for s in steps] == ["click", "click"]
    first, second = steps
    assert first.verify == {"url_contains": "/invoices"} and first.target["dom"]["href"] == "/invoices"
    assert first.target["dom"]["classes"] == ["nav"]  # the generated class is not a selector
    assert first.target["coordinate"]["url"] == URL and first.target["coordinate"]["viewport"] == [1280, 800]
    assert second.target["dom"]["testid"] == "export-invoices" and second.verify == {"changed": True}


def test_secret_fields_become_variables_and_focus_clicks_merge() -> None:
    events = [
        ev("click", "textbox", "Email", 0),
        ev("type", "textbox", "Email", 100, text="me@example.com"),
        ev("click", "textbox", "Password", 200, secret=True),
        ev("type", "textbox", "Password", 300, text=None, secret=True),
        ev("press", "textbox", "Password", 400, key="enter"),
        {"kind": "navigate", "url": f"{BASE}/home", "at": 900},
    ]
    steps, variables = steps_from_events(events, URL)
    assert [s.action for s in steps] == ["type", "type", "press"] and variables == ["PASSWORD"]
    assert steps[0].parameters == {"text": "me@example.com"} and steps[0].verify == {"element": {"name": "Email", "value": "me@example.com"}}
    assert steps[1].parameters == {"variable": "PASSWORD"} and steps[1].verify is None
    assert steps[2].verify == {"url_contains": "/home"}
    workflow = BrowserWorkflow("login", URL, steps, variables)
    with pytest.raises(UsageError, match="PASSWORD"):
        workflow.script({})
    script = workflow.script({"PASSWORD": "pw-123"})
    assert script[0]["action"] == "open" and script[2]["parameters"] == {"text": "pw-123"}


def test_a_typed_url_is_an_open_step() -> None:
    steps, _ = steps_from_events([{"kind": "navigate", "url": URL, "at": 0}, {"kind": "navigate", "url": f"{BASE}/form", "at": 9000}], URL)
    assert [(s.action, s.parameters) for s in steps] == [("open", {"url": f"{BASE}/form"})]


def test_the_record_script_reuses_the_observation_rules() -> None:
    script = record_script()
    assert "const roleOf" in script and "const nameOf" in script and BINDING in script and "HELPERS" not in script
    assert "secretInput(t) ? null" in script  # secret values never leave the page


class FakeConnection:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queue: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        return {}

    def pump(self, **kw: Any) -> None:
        while self.queue:
            message = self.queue.pop(0)
            for listener in list(self.listeners):
                listener(message)


class FakeBrowser:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.navigated: list[str] = []

    def navigate(self, url: str, **kw: Any) -> None:
        self.navigated.append(url)

    def _page(self, cancel: Any) -> tuple[FakeConnection, str]:
        return self.conn, "S1"


def test_the_recorder_listens_through_a_devtools_binding() -> None:
    conn = FakeConnection()
    recorder = BrowserRecorder(FakeBrowser(conn))  # type: ignore[arg-type]
    recorder.start(URL)
    assert [m for m, _ in conn.calls] == ["Runtime.addBinding", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"]
    payload = json.dumps(ev("click", "button", "Export", 5000))
    conn.queue += [
        {"method": "Runtime.bindingCalled", "sessionId": "S1", "params": {"name": BINDING, "payload": payload}},
        {"method": "Runtime.bindingCalled", "sessionId": "OTHER", "params": {"name": BINDING, "payload": payload}},
        {"method": "Runtime.bindingCalled", "sessionId": "S1", "params": {"name": "somethingElse", "payload": payload}},
        {"method": "Runtime.bindingCalled", "sessionId": "S1", "params": {"name": BINDING, "payload": "not json"}},
    ]
    assert recorder.poll() == 2  # the start navigation and the one valid click
    workflow = recorder.workflow("export")
    assert workflow.start_url == URL and [s.target["label"] for s in workflow.steps] == ["Export"]
    recorder.stop()
    assert conn.listeners == []


def test_workflow_storage_and_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WorkflowStore(tmp_path)
    store.save(BrowserWorkflow("export", URL))
    assert store.names() == ["export"] and store.load("export").start_url == URL
    with pytest.raises(NotFoundError):
        store.load("missing")
    with pytest.raises(UsageError):
        store.path("../evil")
    monkeypatch.setenv("HIGHHX_VAR_TOKEN", "t0k")
    assert variables_from(["user=me"], ["TOKEN", "USER"]) == {"USER": "me", "TOKEN": "t0k"}


# ------------------------------------------------------------------ replay
@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWebApp:
    app = FakeWebApp()
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: app))
    return app


def _executor(make_app, root: Path) -> ActionExecutor:
    app = make_app(root)
    gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    return ActionExecutor(app, gate, actor=Actor.USER, sleep=lambda _s: None)


def recorded_export() -> BrowserWorkflow:
    events = [
        {"kind": "navigate", "url": URL, "at": 0},
        ev("click", "link", "Invoices", 1000, attributes={"href": "/invoices", "tag": "a"}),
        {"kind": "navigate", "url": f"{BASE}/invoices", "at": 1500},
        ev("click", "button", "Export", 3000, url=f"{BASE}/invoices", attributes={"testid": "export-invoices", "tag": "button"}),
    ]
    steps, variables = steps_from_events(events)
    return BrowserWorkflow("export", URL, steps, variables)


def test_replay_runs_a_recorded_workflow(web: FakeWebApp, agent_project: Path, make_app, tmp_path: Path) -> None:
    executor = _executor(make_app, agent_project)
    report = replay(executor, recorded_export(), trajectories=TrajectoryStore(tmp_path / "t"), sleep=lambda _s: None)
    assert report.status == "completed" and web.state["exported"] and report.healed == []


def test_replay_heals_selectors_after_a_redesign_and_saves_them(web: FakeWebApp, agent_project: Path, make_app, tmp_path: Path) -> None:
    web.variant = "redesign"  # Invoices → "Billing documents", Export → "Download CSV"
    store = WorkflowStore(tmp_path / "w")
    workflow = recorded_export()
    store.save(workflow)
    executor = _executor(make_app, agent_project)
    report = replay(executor, workflow, workflows=store, trajectories=TrajectoryStore(tmp_path / "t"), sleep=lambda _s: None)
    assert report.status == "completed" and web.state["exported"] and report.saved
    assert [(h["was"], h["now"], h["strategy"]) for h in report.healed] == [
        ("Invoices", "Billing documents", "dom"),
        ("Export", "Download CSV", "dom"),
    ]
    first = report.healed[0]
    assert first["old_selector"]["label"] == "Invoices" and first["new_selector"]["label"] == "Billing documents"
    assert "accessibility" in first["reason"] and 0 < first["confidence"] <= 1
    healed = store.load("export")
    assert healed.heals == 2 and healed.steps[0].target["label"] == "Billing documents"
    assert healed.steps[0].history[-1]["was"] == "Invoices"
    web.state.clear()
    again = replay(executor, healed, workflows=store, sleep=lambda _s: None)  # the healed workflow now matches directly
    assert again.status == "completed" and again.healed == []
