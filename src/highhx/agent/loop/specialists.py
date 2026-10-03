"""Optional specialist agents, coordinated by a supervisor. One agent is the default.

    SupervisorAgent   splits a task into sub-tasks for specialists, runs them in order sharing one
                      executor, one trace and one trajectory store, and checks the result
      ResearchAgent   reads: web pages and API responses (browser reads, api.request GET)
      BrowserAgent    acts in the browser
      ComputerAgent   acts on the desktop
      AndroidAgent    acts on an Android device
      CodeAgent       commands and files in the project (shell, filesystem, git, tests)
      VerifierAgent   checks the overall success criteria, and acts on nothing
      PlannerAgent    turns the task into sub-tasks (a model when one is available, otherwise
                      the tool router)

A specialist is an :class:`~highhx.agent.loop.loop.AgentLoop` with a narrower set of allowed
actions and a surface. It has no other powers and no way around the executor: every action it
takes is classified, policy-checked, approved and audited like any other. Simple tasks (one kind
of tool) never involve the supervisor (:meth:`SupervisorAgent.needed`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.agent.loop.loop import AgentLoop
from highhx.agent.loop.model import AgentTask, LoopResult, Status
from highhx.agent.loop.routing import ToolRouter
from highhx.core.events import new_id, trace_context
from highhx.verification.declarative import Verdict, VerificationContext, verify

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.loop.planner import AgentPlanner
    from highhx.models.interfaces import LanguageModel
    from highhx.trajectories.store import TrajectoryStore


@dataclass(frozen=True)
class Specialist:
    name: str
    surface: str
    allowed: tuple[str, ...]
    description: str

    def task(self, goal: str, parent: AgentTask) -> AgentTask:
        return AgentTask(
            goal,
            surface=self.surface,
            max_steps=parent.max_steps,
            max_failures=parent.max_failures,
            max_replans=parent.max_replans,
            max_recoveries=parent.max_recoveries,
            timeout=parent.timeout,
            allowed=self.allowed,
            ocr=parent.ocr,
            vision=parent.vision,
            remote_vision=parent.remote_vision,
            device=parent.device,
            app=parent.app,
        )


SPECIALISTS: dict[str, Specialist] = {
    s.name: s
    for s in (
        Specialist("research", "browser", ("browser.open", "browser.find", "browser.extract", "browser.wait", "browser.scroll", "api.request", "computer.state"), "reads web pages and APIs"),
        Specialist("browser", "browser", ("browser.", "computer.state"), "acts in the browser"),
        Specialist("computer", "desktop", ("computer.",), "acts on the desktop"),
        Specialist("android", "android", ("android.", "computer.state"), "acts on an Android device"),
        Specialist("code", "none", ("shell.run", "filesystem.", "git.", "project.", "package."), "commands and files in the project"),
    )
}
ROUTE_TO_SPECIALIST = {"browser": "browser", "desktop": "computer", "android": "android", "shell": "code", "code": "code", "filesystem": "code", "api": "research", "sandbox": "code"}


@dataclass
class SubTask:
    specialist: str
    goal: str
    success: dict[str, Any] | list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"specialist": self.specialist, "goal": self.goal, "success": self.success}


@dataclass
class SupervisedResult:
    status: Status
    summary: str
    results: list[tuple[SubTask, LoopResult]] = field(default_factory=list)
    trace_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == Status.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "summary": self.summary,
            "trace_id": self.trace_id,
            "subtasks": [{**s.to_dict(), **r.to_dict()} for s, r in self.results],
        }


class PlannerAgent:
    """Splits a task into specialist sub-tasks. With a model: asks it (JSON, validated against
    the known specialists). Without one: one sub-task per distinct tool the router finds, in
    the order the task mentions them."""

    def __init__(self, model: LanguageModel | None = None, router: ToolRouter | None = None) -> None:
        self.model = model
        self.router = router or ToolRouter()

    def split(self, goal: str) -> list[SubTask]:
        if self.model is not None:
            names = ", ".join(f"{s.name} ({s.description})" for s in SPECIALISTS.values())
            prompt = (
                f"Task: {goal}\nSplit it into 1-5 sequential sub-tasks, each for one specialist: {names}.\n"
                'Answer JSON: {"subtasks": [{"specialist": "...", "goal": "..."}]}'
            )
            reply = self.model.complete("You coordinate specialist agents. Answer JSON only.", prompt, max_tokens=600)
            match = re.search(r"\{.*\}", reply.text, re.S)
            try:
                items = json.loads(match.group(0)).get("subtasks") if match else None
            except ValueError:
                items = None
            subtasks = [
                SubTask(str(i["specialist"]), str(i["goal"]))
                for i in items or []
                if isinstance(i, dict) and i.get("specialist") in SPECIALISTS and str(i.get("goal", "")).strip()
            ]
            if subtasks:
                return subtasks
        seen: list[str] = []
        for route in self.router.route(goal):
            name = ROUTE_TO_SPECIALIST.get(route.tool)
            if route.available and name and name not in seen and route.score >= 1.0:
                seen.append(name)
        return [SubTask(name, goal) for name in seen] or [SubTask("code", goal)]


class VerifierAgent:
    """Checks a task's success criteria on fresh observations of the surfaces they mention. It
    observes through the ``computer.state`` action and never acts."""

    def __init__(self, executor: ActionExecutor) -> None:
        self.executor = executor

    def check(self, success: dict[str, Any] | list[Any] | None, surface: str) -> tuple[Verdict, str]:
        if success is None:
            return Verdict.SATISFIED, "no success criteria given"
        state = None
        if surface in ("desktop", "browser", "android"):
            from highhx.actions.handlers.state import state_from_result

            result = self.executor.run("computer.state", {"surface": surface})
            state = state_from_result(result) if result.ok else None
        report = verify(success, VerificationContext(after=state, root=self.executor.app.root))
        return report.verdict, report.result.detail


class SupervisorAgent:
    def __init__(
        self,
        executor: ActionExecutor,
        planner_for: Callable[[Specialist, str], AgentPlanner],
        *,
        store: TrajectoryStore | None = None,
        splitter: PlannerAgent | None = None,
        router: ToolRouter | None = None,
    ) -> None:
        self.executor = executor
        self.planner_for = planner_for
        self.store = store
        self.router = router or ToolRouter()
        self.splitter = splitter or PlannerAgent(router=self.router)

    def needed(self, goal: str) -> bool:
        return self.router.needs_specialists(goal)

    def run(self, task: AgentTask, subtasks: Sequence[SubTask] | None = None) -> SupervisedResult:
        trace_id = new_id("tr")
        plan = list(subtasks or self.splitter.split(task.goal))
        result = SupervisedResult(Status.RUNNING, "", trace_id=trace_id)
        with trace_context(trace_id=trace_id, source="supervisor"):
            self.executor.events.emit("plan.created", steps=[f"{s.specialist}: {s.goal}" for s in plan], planner="supervisor")
            for sub in plan:
                specialist = SPECIALISTS[sub.specialist]
                loop = AgentLoop(self.executor, self.planner_for(specialist, sub.goal), store=self.store, router=self.router, agent=f"{specialist.name}-agent")
                child = specialist.task(sub.goal, task)
                child.success = sub.success
                outcome = loop.run(child)
                result.results.append((sub, outcome))
                if not outcome.ok:
                    result.status = Status.NEEDS_USER if outcome.status == Status.NEEDS_USER else Status.FAILED
                    result.summary = f"{specialist.name} could not finish {sub.goal!r}: {outcome.summary}"
                    return result
            surface = task.surface if task.surface != "auto" else self.router.surface(task.goal)
            verdict, detail = VerifierAgent(self.executor).check(task.success, surface)
            if verdict == Verdict.SATISFIED:
                result.status, result.summary = Status.COMPLETED, f"{len(plan)} sub-task(s) done; {detail}"
            elif verdict == Verdict.UNKNOWN:
                result.status, result.summary = Status.NEEDS_USER, f"the sub-tasks finished, but success could not be confirmed ({detail})"
            else:
                result.status, result.summary = Status.FAILED, f"the sub-tasks finished, but the task is not achieved ({detail})"
        return result
