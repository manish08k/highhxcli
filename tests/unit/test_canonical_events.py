"""One event system, one vocabulary: older names map to canonical ones on every record; the
executor, agent loop, grounding and browser runtime emit the canonical per-surface events."""

from __future__ import annotations

from typing import Any

from highhx.actions import events as ev
from highhx.core.events import EventBus
from highhx.observability.stream import EventRecorder
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


def test_records_carry_the_canonical_name() -> None:
    bus = EventBus()
    recorder = EventRecorder.attach(bus)
    bus.emit("approval.requested", action="x")
    bus.emit("action.retry", action="x")
    bus.emit("task.paused", task="x")
    names = [(r.to_dict()["event"], r.to_dict()["canonical"]) for r in recorder.snapshot()]
    assert names == [
        ("approval.requested", "approval.required"),
        ("action.retry", "retry.started"),
        ("task.paused", "task.paused"),
    ]
    assert ev.canonical("plan.created") == "planning.completed" and ev.canonical("sandbox.exec") == "sandbox.completed"


def test_the_browser_journal_becomes_bus_events() -> None:
    from types import SimpleNamespace

    from highhx.computer.runtime import ComputerRuntime

    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("*", seen.append)
    runtime = ComputerRuntime.__new__(ComputerRuntime)
    runtime.gate = SimpleNamespace(engine=SimpleNamespace(ctx=SimpleNamespace(events=bus)))
    runtime.provider = SimpleNamespace(
        drain_journal=lambda: [
            {"event": "connected", "detail": "connected to the browser (pid 1)", "state": "connected"},
            {"event": "page_crashed", "detail": "the tab crashed", "state": "recovery_required"},
            {"event": "navigation_confirmed", "detail": "https://x.test/", "state": "connected"},
            {"event": "already_open", "detail": "ignored", "state": "connected"},
        ]
    )
    audit = SimpleNamespace(details={})
    runtime._journal(audit)
    assert [e.name for e in seen] == ["browser.started", "browser.crash", "browser.navigation"]
    assert seen[1].data["kind"] == "page_crashed" and len(audit.details["browser"]) == 4


def test_per_surface_events_from_the_executor(agent_project, make_app, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from pathlib import Path

    import yaml

    from highhx.actions.executor import ActionExecutor
    from highhx.automation.engine.bridge import AutomationBridge
    from highhx.computer.driver import HighhXDriver
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.computer_use.environment import SimulatedDesktop
    from tests.unit.agent.conftest import RecordingUI

    desktop = Path(__file__).resolve().parents[1] / "computer_use" / "tasks" / "_desktop.yaml"
    env = SimulatedDesktop(yaml.safe_load(desktop.read_text()))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    seen: list[Any] = []
    app.ctx.events.subscribe("*", seen.append)
    executor.run("computer.edit", {"op": "select_all"})
    executor.run("computer.clipboard_read", {})
    names = [e.name for e in seen]
    assert names.count("computer.input") == 1  # the edit is input; reading the clipboard is not
    inputs = [e for e in seen if e.name == "computer.input"]
    assert inputs[0].data == {"action": "computer.edit", "status": "ok"}  # no typed text in the payload
