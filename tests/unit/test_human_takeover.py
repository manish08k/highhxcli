"""Human takeover: a person takes the computer, HighhX changes nothing on it (whoever asks), a
running task waits, and after the hand-back the task observes again before its next step — no
earlier screenshot grounds a click and no completed step is repeated."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner, Status
from highhx.automation.engine.bridge import AutomationBridge
from highhx.benchmarks.environments.web import BASE, FakeWebApp
from highhx.computer.capture import StaleCapture
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from highhx.perception.png import encode, solid
from highhx.safety.actions import Actor
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401


class FakePage:
    def viewport(self, *, cancel: Any = None) -> dict[str, Any]:
        return {"url": f"{BASE}/", "title": "Shop", "x": 0, "y": 0, "width": 20, "height": 20, "dpr": 1}

    def page_capture(self, *, cancel: Any = None) -> tuple[bytes, dict[str, Any]]:
        return encode(20, 20, bytes(solid(20, 20))), self.viewport()


DESKTOP = Path(__file__).resolve().parents[1] / "computer_use" / "tasks" / "_desktop.yaml"


@pytest.fixture
def kit(agent_project: Path, make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:  # noqa: F811
    web = FakeWebApp()
    env = SimulatedDesktop(yaml.safe_load(DESKTOP.read_text()))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: web))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    app = make_app(agent_project)
    gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK)
    session = ComputerSession(gate, actor=Actor.USER)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session, sleep=lambda _s: None)
    events: list[Any] = []
    app.ctx.events.subscribe("*", events.append)
    return executor, session, web, env, events, TrajectoryStore(tmp_path / "trajectories")


def test_while_a_person_has_the_computer_highhx_changes_nothing(kit: Any) -> None:
    executor, session, _web, _env, _events, _store = kit
    page = FakePage()
    capture = session.captures.take_page(page)
    session.take_over("the tester")
    held = executor.run("computer.click_at", {"x": 10, "y": 10})
    assert held.status == "blocked" and "the tester has control of the computer" in held.error
    assert executor.run("browser.open", {"url": f"{BASE}/invoices"}).status == "blocked"
    assert executor.run("computer.observe", {}).ok  # looking is still allowed (live view, state)
    assert session.release() >= 0 and not session.taken_over
    # a screenshot from before the takeover no longer grounds a click
    with pytest.raises(StaleCapture, match="took control of the computer"):
        session.captures.ground_page(page, capture.id, 5, 5)
    assert executor.run("computer.click_at", {"x": 10, "y": 10}).ok  # handed back: HighhX acts again


def test_a_running_task_waits_then_observes_again_and_repeats_nothing(kit: Any) -> None:
    executor, session, web, _env, events, store = kit
    script = [
        {"action": "open", "parameters": {"url": f"{BASE}/invoices"}, "intent": "open the invoices"},
        {"action": "click", "target": {"label": "Export", "role": "button"}, "intent": "export"},
    ]

    def after_first_step(event: Any) -> None:  # the person takes the computer once step 1 is done
        if event.data.get("action") == "browser.open" and not session.taken_over:
            session.take_over("the tester")

    def when_waiting(_event: Any) -> None:  # and hands it back a little after the task stopped for them
        threading.Timer(0.4, session.release).start()

    unsubscribe = executor.app.ctx.events.subscribe("action.completed", after_first_step)
    unsubscribe_paused = executor.app.ctx.events.subscribe("task.paused", when_waiting)
    started = time.monotonic()
    result = AgentLoop(executor, ScriptedPlanner(script), store=store, sleep=lambda _s: None).run(
        AgentTask("export the invoices", surface="browser", success={"text": "Export ready"})
    )
    unsubscribe()
    unsubscribe_paused()
    assert result.status == Status.COMPLETED and web.state["exported"]
    assert time.monotonic() - started >= 0.35  # it waited for the hand-back
    resumed = [e for e in events if e.name == "task.resumed"]
    assert resumed and resumed[0].data.get("after_takeover") is True
    actions = [e.data.get("action") for e in events if e.name == "action.completed"]
    assert actions.count("browser.open") == 1  # the step done before the takeover is not repeated
    assert [s.outcome for s in store.load(result.trajectory.id).steps] == ["success", "success"]
