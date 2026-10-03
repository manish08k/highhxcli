"""The live dashboard is a view of the event stream: it folds events into what is shown and never
executes anything. Approval prompts pause it."""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from highhx.core.events import EventBus, trace_context
from highhx.observability.stream import EventRecorder
from highhx.ui.live import DashboardState, LiveDashboard, PausingPrompter, render


def text_of(state: DashboardState) -> str:
    console = Console(file=io.StringIO(), width=140, record=True, color_system=None)
    console.print(render(state))
    return console.export_text()


def run_events(bus: EventBus) -> None:
    with trace_context(trace_id="tr_demo", task_id="task_demo"):
        bus.emit("task.started", task="export the invoices", surface="browser")
        bus.emit("plan.created", steps=["open the invoices", "export the invoices"])
        bus.emit("plan.updated", items=[{"title": "open the invoices", "status": "done"}, {"title": "export the invoices", "status": "running"}])
        bus.emit("observation.created", surface="browser", url="https://shop.test/invoices", elements=3)
        bus.emit("grounding.started", target="Export")
        bus.emit("grounding.attempt", strategy="accessibility", result="failed", best=0.0)
        bus.emit("grounding.attempt", strategy="dom", result="success", best=0.95)
        bus.emit("action.planned", action="browser.click", risk="high", asks=True, inputs={"target": 'button:"Export"'})
        bus.emit("approval.requested", action="browser.click", risk="high")
        bus.emit("approval.granted", action="browser.click", risk="high")
        bus.emit("action.completed", action="browser.click", seconds=0.31)
        bus.emit("selector.healed", was="Export", now="Download CSV", strategy="dom")
        bus.emit("verification.completed", outcome="success")
        bus.emit("model.usage", model="m", input_tokens=1000, output_tokens=240, cost=0.002)
        bus.emit("action.failed", action="browser.press", status="failed", error="no focus")


def test_events_drive_every_panel() -> None:
    bus = EventBus()
    recorder = EventRecorder.attach(bus)
    state = DashboardState(project="shop", branch="main", plan_name="Pro", account="me@example.com", connection="computer: local")
    recorder.listen(state.apply)
    run_events(bus)
    text = text_of(state)
    for expected in (
        "HighhX", "shop", "main", "Pro", "me@example.com", "computer: local",
        "export the invoices", "running",
        "✓ open the invoices", "● export the invoices", "[1/2]",
        "browser.click", "risk high", "approved",
        "https://shop.test/invoices", "3 element(s)",
        "accessibility ✗", "dom ✓ 0.95",
        "success", "1 healed",
        "✓ browser.click 0.3s", "✗ browser.press",
        "1,240 tokens", "$0.0020",
        "healed 'Export' → 'Download CSV'", "no focus",
        "trace tr_demo",
    ):
        assert expected in text, (expected, text)


def test_task_end_and_observations_are_not_shown_as_tools() -> None:
    state = DashboardState()
    bus = EventBus()
    EventRecorder.attach(bus).listen(state.apply)
    with trace_context(trace_id="tr_x"):
        bus.emit("task.started", task="t")
        bus.emit("action.planned", action="computer.state", risk="low", asks=False)
        bus.emit("action.completed", action="computer.state", seconds=0.01)
        bus.emit("task.failed", status="failed", summary="no progress")
    assert state.actions == 0 and not state.tools and state.status == "failed"
    assert "no progress" in text_of(state)


class FakeDashboard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")


class FakePrompter:
    interactive = True

    def confirm_action(self, request: Any) -> bool:
        return True

    def ask_permission(self, action: str, details: Any, *, allow_always: bool = True) -> str:
        return "no"

    def notice(self, level: str, message: str) -> None:
        pass


def test_prompts_pause_the_live_view() -> None:
    dashboard = FakeDashboard()
    prompter = PausingPrompter(FakePrompter(), dashboard)  # type: ignore[arg-type]
    assert prompter.confirm_action(object()) is True and prompter.ask_permission("x", []) == "no"
    assert dashboard.calls == ["pause", "resume", "pause", "resume"] and prompter.interactive


def test_the_live_dashboard_prints_its_final_state_without_a_terminal() -> None:
    bus = EventBus()
    recorder = EventRecorder.attach(bus)
    out = io.StringIO()
    console = Console(file=out, width=120, color_system=None)
    with LiveDashboard(console, recorder) as live:
        run_events(bus)
        assert live.state.task == "export the invoices"
    assert "export the invoices" in out.getvalue()
