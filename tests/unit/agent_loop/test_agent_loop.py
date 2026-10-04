"""The agent loop end to end on a simulated web app and desktop: planning, grounding, the executor,
verification, reflection, recovery (re-observe, scroll, re-plan), safety (declines are final,
risky actions are never retried silently), checkpoints and resume, trajectories, the model
planner, tool routing and specialists."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.actions.executor import ActionExecutor
from highhx.agent.loop import AgentLoop, AgentTask, ModelPlanner, ScriptedPlanner, Status, ToolRouter, resume
from highhx.agent.loop.model import Decision
from highhx.agent.loop.specialists import SPECIALISTS, PlannerAgent, SubTask, SupervisorAgent
from highhx.agent.model.capabilities import ModelCapabilities
from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver
from highhx.computer.session import ComputerSession
from highhx.models import ChatLanguageModel
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from highhx.trajectories import TrajectoryStore, replay_steps
from tests.computer_use.environment import SimulatedDesktop
from tests.unit.agent.conftest import RecordingUI, ScriptedProvider, reply
from tests.unit.agent_loop.fake_web import BASE, FakeWebApp

DESKTOP = Path(__file__).resolve().parents[2] / "computer_use" / "tasks" / "_desktop.yaml"


class Kit:
    def __init__(self, executor: ActionExecutor, ui: RecordingUI, web: FakeWebApp, store: TrajectoryStore, events: list[Any]) -> None:
        self.executor, self.ui, self.web, self.store, self.events = executor, ui, web, store, events

    def loop(self, planner: Any, **kw: Any) -> AgentLoop:
        return AgentLoop(self.executor, planner, store=self.store, sleep=lambda _s: None, **kw)

    def names(self) -> list[str]:
        return [e.name for e in self.events]


@pytest.fixture
def kit(agent_project: Path, make_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Kit:
    web = FakeWebApp()
    env = SimulatedDesktop(yaml.safe_load(DESKTOP.read_text()))
    driver = HighhXDriver(AutomationBridge(env))
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: web))
    monkeypatch.setattr(ComputerSession, "driver", lambda self: driver)
    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    executor = ActionExecutor(app, gate, actor=Actor.USER, sleep=lambda _s: None)
    events: list[Any] = []
    app.ctx.events.subscribe("*", events.append)
    kit = Kit(executor, ui, web, TrajectoryStore(tmp_path / "trajectories", redactor=app.redactor), events)
    kit.desktop = env  # type: ignore[attr-defined]
    return kit


def export_script(**target: Any) -> list[dict[str, Any]]:
    return [
        {"action": "open", "parameters": {"url": f"{BASE}/invoices"}, "intent": "open the invoices"},
        {"action": "click", "target": {"label": "Export", "role": "button", **target}, "intent": "export the invoices"},
    ]


# ----------------------------------------------------------------- happy path
def test_a_scripted_browser_task_runs_verifies_and_is_recorded(kit: Kit) -> None:
    task = AgentTask("export the invoices", surface="browser", success={"text": "Export ready"})
    result = kit.loop(ScriptedPlanner(export_script())).run(task)
    assert result.status == Status.COMPLETED and kit.web.state["exported"]
    trajectory = kit.store.load(result.trajectory.id)
    assert [s.outcome for s in trajectory.steps] == ["success", "success"]
    assert trajectory.steps[1].action["action_type"] == "browser.click"
    assert trajectory.steps[1].action["parameters"]["target"] == 'button:"Export"'
    assert trajectory.steps[1].grounding["candidate"]["strategy"] == "accessibility"
    assert [p["status"] for p in trajectory.plan] == ["done", "done"]
    names = kit.names()
    for name in ("agent.started", "plan.created", "grounding.completed", "action.completed", "verification.completed", "checkpoint.created", "task.started", "task.completed", "agent.completed"):
        assert name in names, name
    task_events = [e for e in kit.events if e.context.get("task_id") == result.trajectory.id]
    assert task_events and all(e.context.get("trace_id") == result.trajectory.trace_id for e in task_events)
    assert result.metrics["actions"] == 2 and result.metrics["grounding"] == {"accessibility": 1}


def test_a_redesigned_site_is_healed_through_other_selectors(kit: Kit) -> None:
    kit.web.variant = "redesign"  # "Export" is now "Download CSV"; only its data-testid stayed
    script = export_script(dom={"testid": "export-invoices", "tag": "button"}, accessibility={"name": "Export", "role": "button"})
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("export", surface="browser", success={"text": "Export ready"}))
    assert result.status == Status.COMPLETED
    step = result.trajectory.steps[-1]
    attempts = {a["strategy"]: a["result"] for a in step.grounding["attempts"]}
    assert attempts["accessibility"] == "failed" and attempts["dom"] == "success"
    assert step.action["parameters"]["target"] == 'button:"Download CSV"'


def test_an_off_screen_target_is_found_after_looking_again_and_scrolling(kit: Kit) -> None:
    kit.web.hidden_until_scroll = True
    result = kit.loop(ScriptedPlanner(export_script())).run(AgentTask("export", surface="browser", success={"text": "Export ready"}))
    assert result.status == Status.COMPLETED and ("scroll", "down") in kit.web.log
    decisions = [s.reflection["decision"] for s in result.trajectory.steps]
    assert decisions == ["continue", "reobserve", "scroll", "continue"]
    assert kit.names().count("recovery.started") == 2


# --------------------------------------------------------------------- safety
def test_a_declined_dangerous_click_ends_the_task(kit: Kit) -> None:
    kit.ui.action_answers = [False]
    script = [{"action": "click", "target": {"label": "Delete account", "role": "button"}}]
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("tidy up", surface="browser"))
    assert result.status == Status.FAILED and "denied" in result.summary
    assert not kit.web.state.get("deleted") and kit.ui.requests[0].risk_name in ("high", "critical")
    assert result.trajectory.steps[-1].reflection["decision"] == "stop"


def test_risky_actions_that_fail_verification_are_never_retried_silently(kit: Kit) -> None:
    kit.web.url = f"{BASE}/form"
    script = [{"action": "click", "target": {"label": "Submit", "role": "button"}}]  # no email: nothing happens
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("submit", surface="browser", max_replans=2))
    assert result.status == Status.FAILED
    decisions = [s.reflection["decision"] for s in result.trajectory.steps]
    assert "retry" not in decisions and decisions.count("replan") == 3
    # every attempt was approved again (the browser runtime also confirms the element itself), never repeated silently
    assert len(kit.ui.requests) >= 3


def test_typing_is_verified_and_the_task_success_is_checked(kit: Kit) -> None:
    kit.web.url = f"{BASE}/form"
    script = [
        {"action": "type", "target": {"label": "Email", "role": "textbox"}, "parameters": {"text": "me@example.com"}},
        {"action": "click", "target": {"label": "Submit"}},
    ]
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("sign up", surface="browser", success={"text": "Thank you"}))
    assert result.status == Status.COMPLETED and kit.web.state["submitted"]
    assert result.trajectory.steps[0].action["action_type"] == "browser.fill"
    assert result.trajectory.steps[0].action["parameters"]["text"] == "<14 characters>"  # typed text is not stored


def test_a_task_whose_success_check_fails_is_not_reported_done(kit: Kit) -> None:
    result = kit.loop(ScriptedPlanner([{"action": "open", "parameters": {"url": f"{BASE}/invoices"}}])).run(
        AgentTask("export", surface="browser", success={"text": "Export ready"})
    )
    assert result.status == Status.FAILED and "not achieved" in result.summary


# --------------------------------------------------------------- no screen
def test_tasks_without_a_screen_use_catalog_actions(kit: Kit, agent_project: Path) -> None:
    script = [
        {"action": "filesystem.write", "parameters": {"path": "notes/plan.txt", "content": "ship it\n"}},
        {"action": "filesystem.read", "parameters": {"path": "notes/plan.txt"}},
    ]
    task = AgentTask("write the plan", surface="none", success={"file": {"path": "notes/plan.txt", "contains": "ship"}})
    result = kit.loop(ScriptedPlanner(script)).run(task)
    assert result.status == Status.COMPLETED and (agent_project / "notes" / "plan.txt").read_text() == "ship it\n"
    assert [s.outcome for s in result.trajectory.steps] == ["success", "success"]


def test_disallowed_actions_are_refused(kit: Kit) -> None:
    script = [{"action": "shell.run", "parameters": {"command": "echo hi"}}]
    task = AgentTask("x", surface="none", allowed=("filesystem.",), max_replans=0)
    result = kit.loop(ScriptedPlanner(script)).run(task)
    assert result.status == Status.FAILED and "not allowed" in result.trajectory.steps[0].result["error"]


def test_step_and_time_limits(kit: Kit) -> None:
    class Forever:
        name = "forever"

        def outline(self, task, state):  # type: ignore[no-untyped-def]
            return []

        def next(self, task, state, history, *, feedback="", lessons=()):  # type: ignore[no-untyped-def]
            from highhx.agent.loop.model import StepIntent

            return Decision("act", StepIntent("filesystem.list", parameters={}))

        def state(self) -> dict[str, Any]:
            return {"kind": "scripted", "steps": [], "cursor": 0}

    result = kit.loop(Forever()).run(AgentTask("loop", surface="none", max_steps=4))
    assert result.status == Status.FAILED and "4 steps" in result.summary


# ------------------------------------------------------------ checkpoints
def test_an_interrupted_task_resumes_from_its_checkpoint(kit: Kit) -> None:
    class Interrupting(ScriptedPlanner):
        def next(self, task, state, history, **kw):  # type: ignore[no-untyped-def]
            if self.cursor == 1:
                raise KeyboardInterrupt
            return super().next(task, state, history, **kw)

    task = AgentTask("export", surface="browser", success={"text": "Export ready"})
    first = kit.loop(Interrupting(export_script())).run(task)
    assert first.status == Status.INTERRUPTED and "--resume" in first.summary
    saved = kit.store.load(first.trajectory.id)
    assert saved.status == "interrupted" and saved.metrics["checkpoint"]["planner"]["cursor"] == 1
    second = resume(kit.executor, kit.store, first.trajectory.id, sleep=lambda _s: None)
    assert second.status == Status.COMPLETED and second.trajectory.id == first.trajectory.id
    assert second.trajectory.trace_id == first.trajectory.trace_id and len(second.trajectory.steps) == 2
    with pytest.raises(Exception, match="only unfinished"):
        resume(kit.executor, kit.store, first.trajectory.id)


def test_trajectories_replay_semantically(kit: Kit) -> None:
    first = kit.loop(ScriptedPlanner(export_script())).run(AgentTask("export", surface="browser", success={"text": "Export ready"}))
    steps = replay_steps(kit.store.load(first.trajectory.id))
    assert steps[1]["target"]["label"] == "Export" and "x" not in steps[1]["parameters"]
    kit.web.state.clear()
    kit.web.variant = "redesign"
    again = kit.loop(ScriptedPlanner(steps)).run(AgentTask("export again", surface="browser", success={"text": "Export ready"}))
    assert again.status == Status.COMPLETED, again.trajectory.describe() + str(steps)  # healed through the recorded data-testid
    hits = kit.store.search("export the invoices")
    assert hits and hits[0].trajectory.task.startswith("export")


# -------------------------------------------------------------- model planner
CAPS = ModelCapabilities("local", "planner-1", True, 4, 32000, "json", "pixels", True)


def test_the_model_planner_proposes_steps_that_the_executor_runs(kit: Kit) -> None:
    from highhx.agent.messages import Usage

    provider = ScriptedProvider(
        [
            reply('{"steps": ["open invoices", "export"]}'),
            reply("I will click it."),  # no JSON: asked again
            reply('{"thought": "open", "action": {"action": "open", "parameters": {"url": "https://shop.test/invoices"}}}', usage=Usage(200, 10)),
            reply('{"thought": "export", "action": {"action": "click", "target": {"label": "Export", "role": "button"}}}', usage=Usage(300, 12)),
            reply('{"thought": "done", "done": true, "summary": "Export ready is shown"}'),
        ]
    )
    planner = ModelPlanner(ChatLanguageModel(provider, CAPS), kit.executor.catalog)
    result = kit.loop(planner).run(AgentTask("export the invoices", surface="browser", success={"text": "Export ready"}))
    assert result.status == Status.COMPLETED and kit.web.state["exported"]
    assert result.metrics["tokens_in"] >= 500 and "model.usage" in kit.names()
    assert "untrusted content" in provider.requests[-1].messages[0].blocks[-1].text


def test_the_model_planner_cannot_invent_actions_or_skip_checks(kit: Kit) -> None:
    provider = ScriptedProvider(
        [
            reply('{"steps": []}'),
            reply('{"action": {"action": "os.format_disk"}}'),
            reply('{"action": {"action": "os.format_disk"}}'),
            reply('{"action": {"action": "os.format_disk"}}'),
        ]
    )
    planner = ModelPlanner(ChatLanguageModel(provider, CAPS), kit.executor.catalog)
    result = kit.loop(planner).run(AgentTask("x", surface="browser"))
    assert result.status == Status.FAILED and "no usable step" in result.summary
    asked = ScriptedProvider([reply('{"steps": []}'), reply('{"ask_user": "Which account?"}')])
    result = kit.loop(ModelPlanner(ChatLanguageModel(asked, CAPS), kit.executor.catalog)).run(AgentTask("x", surface="browser"))
    assert result.status == Status.NEEDS_USER and result.summary == "Which account?"


# ------------------------------------------------------------------- desktop
def test_a_desktop_task_goes_through_the_computer_api(kit: Kit) -> None:
    script = [
        {"action": "type", "target": {"label": "Title", "role": "textbox"}, "parameters": {"text": "Plan"}},
        {"action": "type", "target": {"label": "Body", "role": "textbox"}, "parameters": {"text": "Ship on Friday"}},
    ]
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("fill the note", surface="desktop"))
    assert result.status == Status.COMPLETED, result.trajectory.describe()
    assert kit.desktop.element("Title")["value"] == "Plan" and kit.desktop.element("Body")["value"] == "Ship on Friday"  # type: ignore[attr-defined]
    calls = [name for name, _args in kit.desktop.log]  # type: ignore[attr-defined]
    assert calls.count("click_at") + calls.count("click") >= 2 and calls.count("type") == 2  # each field focused by a grounded click, then typed


# ------------------------------------------------------------------- routing
@pytest.mark.parametrize(
    ("goal", "tool", "surface"),
    [
        ("Calculate the average price from prices.csv", "filesystem", "none"),
        ("Click the export button", "browser", "browser"),
        ("Remove the background in Photoshop", "desktop", "desktop"),
        ("Run the tests", "shell", "none"),
        ("Create a contact on the Android phone", "android", "android"),
    ],
)
def test_tool_routing(goal: str, tool: str, surface: str) -> None:
    router = ToolRouter()
    assert router.route(goal)[0].tool == tool and router.surface(goal) == surface


def test_routing_prefers_available_tools_and_flags_vision() -> None:
    router = ToolRouter({"android": lambda: (False, "adb is not installed")})
    routes = router.route("open the settings app on the android phone")
    assert routes[-1].tool == "android" and not routes[-1].available and "adb" in routes[-1].reason
    assert ToolRouter().route("Remove the background in Photoshop")[0].needs_vision
    assert not ToolRouter().needs_specialists("Run the tests")
    assert ToolRouter().needs_specialists("Find the API docs on the website, then fix the bug in the client code")


# --------------------------------------------------------------- specialists
def test_the_supervisor_coordinates_specialists_through_the_same_executor(kit: Kit, agent_project: Path) -> None:
    scripts = {
        "browser": export_script(),
        "code": [{"action": "filesystem.write", "parameters": {"path": "export.log", "content": "exported\n"}}],
    }
    supervisor = SupervisorAgent(kit.executor, lambda spec, goal: ScriptedPlanner(scripts[spec.name]), store=kit.store)
    plan = [SubTask("browser", "export the invoices", {"text": "Export ready"}), SubTask("code", "log it")]
    result = supervisor.run(AgentTask("export and log", surface="none", success={"file": {"path": "export.log", "contains": "exported"}}), plan)
    assert result.status == Status.COMPLETED and kit.web.state["exported"]
    assert [r.trajectory.agent for _s, r in result.results] == ["browser-agent", "code-agent"]
    started = [e for e in kit.events if e.name == "agent.started"]
    assert {e.context["source"] for e in started} == {"browser-agent", "code-agent"}


def test_a_specialist_cannot_leave_its_lane(kit: Kit) -> None:
    supervisor = SupervisorAgent(kit.executor, lambda spec, goal: ScriptedPlanner([{"action": "shell.run", "parameters": {"command": "echo hi"}}]), store=kit.store)
    result = supervisor.run(AgentTask("browse", surface="browser"), [SubTask("research", "read the docs")])
    assert result.status == Status.FAILED and "research" in result.summary
    assert SPECIALISTS["research"].allowed and not SPECIALISTS["research"].task("x", AgentTask("y")).permits("shell.run")


def test_the_planner_agent_splits_by_tool_without_a_model() -> None:
    names = [s.specialist for s in PlannerAgent().split("Find the API docs on the website, then fix the bug in the client code")]
    assert names[0] == "code" and {"research", "browser"} & set(names)
    assert [s.specialist for s in PlannerAgent().split("Run the tests")] == ["code"]


def test_a_click_without_a_visible_effect_is_not_reported_done(kit: Kit) -> None:
    script = [{"action": "click", "target": {"label": "Increment", "role": "button"}}]  # its count is not in the tree
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("press it", surface="desktop", max_replans=1))
    assert result.status == Status.FAILED and result.trajectory.steps[0].outcome != "success"


def test_text_typed_into_a_secret_field_is_never_stored(kit: Kit) -> None:
    kit.web.url = f"{BASE}/form"
    script = [{"action": "type", "target": {"label": "Password", "role": "textbox"}, "parameters": {"text": "hunter2-secret"}}]
    result = kit.loop(ScriptedPlanner(script)).run(AgentTask("log in", surface="browser"))
    assert kit.web.state["password"] == "hunter2-secret"
    raw = kit.store.path(result.trajectory.id).read_text()
    assert "hunter2-secret" not in raw and "<14 characters>" in raw
    checkpoint_step = kit.store.load(result.trajectory.id).metrics["checkpoint"]["planner"]["steps"][0]
    assert checkpoint_step["parameters"] == {"text": None, "__redacted__": True}
    # a resumed copy of this task asks for the text again instead of typing a placeholder
    redo = kit.loop(ScriptedPlanner([checkpoint_step])).run(AgentTask("log in again", surface="browser"))
    assert redo.status == Status.NEEDS_USER and "not stored" in redo.summary


def test_a_task_trace_reconstructs_the_run(kit: Kit, tmp_path: Path) -> None:
    from highhx.observability.tasktrace import TraceStore

    traces = TraceStore(tmp_path / "traces")
    kit.web.variant = "redesign"
    script = export_script(dom={"testid": "export-invoices", "tag": "button"}, accessibility={"name": "Export", "role": "button"})
    result = AgentLoop(kit.executor, ScriptedPlanner(script), store=kit.store, traces=traces, sleep=lambda _s: None).run(
        AgentTask("export the invoices", surface="browser", success={"text": "Export ready"})
    )
    trace = traces.load(result.trajectory.trace_id)
    assert trace.goal == "export the invoices" and trace.status == "completed" and trace.task_id == result.trajectory.id
    assert traces.load(result.trajectory.id).trace_id == trace.trace_id  # by task id too
    text = trace.render()
    for expected in ("Plan", "Step", "export the invoices", "Grounding", "accessibility failed → dom success", "Healed", "Download CSV", "Action", "browser.click", "execution", "Verification", "Result"):
        assert expected in text, (expected, text)
    names = [r.name for r in trace.records]
    assert "selector.healed" in names and "agent.reflection" in names and "task.completed" in names
    assert all(r.trace_id == trace.trace_id for r in trace.records)
    action = next(r for r in trace.records if r.name == "action.completed" and r.payload.get("action") == "browser.click")
    assert action.execution_id and action.step_id and action.action_id
    listed = traces.recent()
    assert listed[0]["trace_id"] == trace.trace_id and listed[0]["status"] == "completed"
    state = next(r for r in kit.events if r.name == "observation.created" and r.context.get("step_id"))
    assert state.context.get("task_id") == result.trajectory.id


def test_routing_weighs_success_history_and_risk() -> None:
    goal = "click the export button on the page and run the tests"
    plain = ToolRouter().route(goal)
    assert [r.tool for r in plain] == ["shell", "browser"] and plain[0].cost < plain[1].cost  # a tie: the cheaper first
    remembered = ToolRouter(history={"browser": 1.0, "shell": 0.0}).route(goal)
    assert remembered[0].tool == "browser" and "100% of past tasks succeeded" in remembered[0].reason
    from highhx.agent.loop.routing import success_history
    from highhx.trajectories import Trajectory

    class Store:
        def recent(self, limit: int = 0) -> list[Trajectory]:
            return [Trajectory("t", "browser", status="completed")] * 3 + [Trajectory("t", "none", status="failed")] * 3

    assert success_history(Store()) == {"browser": 1.0, "shell": 0.0}


def test_testing_and_debugging_specialists_are_read_mostly() -> None:
    assert SPECIALISTS["testing"].task("x", AgentTask("y")).permits("project.test")
    assert not SPECIALISTS["testing"].task("x", AgentTask("y")).permits("filesystem.write")
    assert not SPECIALISTS["debugging"].task("x", AgentTask("y")).permits("git.push")
