"""Test doubles for the agent: a scripted model provider and a recording UI."""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.context import gather
from highhx.agent.history import SessionStore
from highhx.agent.memory import ProjectMemory
from highhx.agent.messages import Message, StopReason, TextBlock, ToolCall, Usage
from highhx.agent.model.base import ModelRequest
from highhx.agent.model.resilience import RetryPolicy
from highhx.agent.permissions import ApprovalMode
from highhx.agent.planner import Plan
from highhx.agent.session import AgentSession
from highhx.agent.settings import AgentSettings
from highhx.agent.streaming import Completed, ModelEvent, TextDelta, ToolCallStarted
from highhx.agent.tools.base import Tool, ToolResult
from highhx.cloud.plans import PLANS, PRO
from highhx.commands import App
from highhx.core.context import Options
from highhx.core.errors import ModelProviderError
from highhx.execution.cancellation import CancellationToken
from tests.conftest import copy_fixture

PY = sys.executable
_ids = itertools.count(1)


def reply(
    text: str = "",
    calls: Sequence[tuple[str, dict[str, Any]]] = (),
    *,
    stop: StopReason | None = None,
    usage: Usage | None = None,
) -> list[ModelEvent]:
    """Events for one model response."""
    events: list[ModelEvent] = []
    blocks: list[Any] = []
    if text:
        half = len(text) // 2
        events += [TextDelta(text[:half]), TextDelta(text[half:])]
        blocks.append(TextBlock(text))
    for name, args in calls:
        call_id = f"call_{next(_ids)}"
        events.append(ToolCallStarted(call_id, name))
        blocks.append(ToolCall(call_id, name, args))
    stop_reason: StopReason = stop or ("tool_use" if calls else "end_turn")
    events.append(Completed(Message("assistant", blocks), stop_reason, usage or Usage(100, 20), "scripted-1"))
    return events


Step = list[ModelEvent] | Callable[[ModelRequest], list[ModelEvent]] | Exception


class ScriptedProvider:
    """Replays scripted responses and records every request it receives."""

    name = "scripted"
    default_model = "scripted-1"

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest, *, cancel: CancellationToken | None = None) -> Iterator[ModelEvent]:
        self.requests.append(
            ModelRequest(
                request.system,
                [Message.from_dict(m.to_dict()) for m in request.messages],
                list(request.tools),
                request.model,
                request.max_tokens,
                attempt_id=request.attempt_id,
            )
        )
        if not self.steps:
            yield from reply("(script exhausted)")
            return
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        events = step(request) if callable(step) else step
        yield from events

    def last_tool_results(self) -> list[Any]:
        return self.requests[-1].messages[-1].tool_results


@dataclass
class RecordingUI:
    """AgentUI double. Answers come from queues (default: approve everything)."""

    interactive: bool = True
    permission_answers: list[str] = field(default_factory=list)
    plan_answers: list[tuple[bool, str]] = field(default_factory=list)
    confirm_answers: list[bool] = field(default_factory=list)
    action_answers: list[bool] = field(default_factory=list)
    """Answers to sensitive-action confirmations (default: approve)."""
    default_action_answer: bool = True
    requests: list[Any] = field(default_factory=list)
    default_permission: str = "yes"
    events: list[tuple[str, Any]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def assistant_started(self) -> None:
        self.events.append(("assistant_started", None))

    def assistant_text(self, delta: str) -> None:
        self.text.append(delta)

    def assistant_finished(self) -> None:
        self.events.append(("assistant_finished", None))

    def tool_started(self, tool: Tool | None, call: ToolCall, description: str) -> None:
        self.events.append(("tool_started", call.name))

    def tool_output(self, line: str) -> None:
        self.events.append(("output", line))

    def tool_finished(self, tool: Tool | None, call: ToolCall, result: ToolResult, seconds: float) -> None:
        self.events.append(("tool_finished", (call.name, result.ok, result.summary)))

    def present_plan(self, plan: Plan) -> tuple[bool, str]:
        self.events.append(("plan", plan.goal))
        return self.plan_answers.pop(0) if self.plan_answers else (True, "")

    def plan_updated(self, plan: Plan, index: int) -> None:
        self.events.append(("plan_updated", (index, plan.steps[index].status)))

    def ask_permission(self, action: str, details: Sequence[str], *, allow_always: bool = True) -> str:
        self.events.append(("permission", action))
        return self.permission_answers.pop(0) if self.permission_answers else self.default_permission

    def confirm(self, message: str, *, default: bool = False) -> bool:
        self.events.append(("confirm", message))
        return self.confirm_answers.pop(0) if self.confirm_answers else True

    def confirm_typed(self, message: str, expected: str) -> bool:
        self.events.append(("confirm_typed", message))
        return self.confirm_answers.pop(0) if self.confirm_answers else True

    def confirm_action(self, request: Any) -> bool:
        self.requests.append(request)
        self.events.append(("confirm_action", request.action))
        return self.action_answers.pop(0) if self.action_answers else self.default_action_answer

    def notice(self, level: str, message: str) -> None:
        self.events.append((f"notice:{level}", message))

    def of(self, kind: str) -> list[Any]:
        return [value for name, value in self.events if name == kind]


PRO_FEATURES = PLANS[PRO].features


@pytest.fixture
def agent_project(tmp_path: Path) -> Path:
    """An initialized copy of the Python fixture project whose tests run with this interpreter."""
    root = copy_fixture("python_project", tmp_path)
    (root / ".highhx").mkdir()
    (root / ".highhx" / "config.yaml").write_text(
        "version: 1\nproject:\n  name: pyapp\ncommands:\n"
        f"  test: {PY} -m pytest -q -p no:cacheprovider\n"
        f"  lint: {PY} -c \"print('lint ok')\"\n"
    )
    return root


@pytest.fixture
def make_app() -> Iterator[Callable[..., App]]:
    apps: list[App] = []

    def _make(root: Path, **options: Any) -> App:
        app = App(Options(interactive=options.pop("interactive", True), **options), cwd=root)
        apps.append(app)
        return app

    yield _make
    for app in apps:
        app.close()


@pytest.fixture
def make_session(make_app: Callable[..., App]) -> Callable[..., tuple[AgentSession, ScriptedProvider, RecordingUI]]:
    def _make(
        root: Path,
        steps: Sequence[Step],
        *,
        ui: RecordingUI | None = None,
        mode: ApprovalMode = ApprovalMode.ASK,
        features: frozenset[str] = PRO_FEATURES,
        max_steps: int = 30,
        store: bool = True,
        **options: Any,
    ) -> tuple[AgentSession, ScriptedProvider, RecordingUI]:
        app = make_app(root, **options)
        ui = ui or RecordingUI(interactive=app.options.is_interactive())
        provider = ScriptedProvider(steps)
        session_store = SessionStore(app.db, app.redactor) if store and app.db is not None else None
        record = (
            session_store.create(title="New session", root=str(app.root), provider="scripted", model=None)
            if session_store
            else None
        )
        session = AgentSession(
            app,
            provider,
            AgentSettings(provider="scripted", approval=mode, max_steps=max_steps),
            ui,
            features=features,
            context=gather(app),
            memory=ProjectMemory.for_project(app.root, initialized=app.initialized, redactor=app.redactor),
            store=session_store,
            record=record,
            sleep=lambda _s: None,
        )
        session.retry = RetryPolicy(base_delay=0.01, max_delay=0.02)
        return session, provider, ui

    return _make


def retryable(message: str = "overloaded") -> ModelProviderError:
    return ModelProviderError(message, retryable=True)
