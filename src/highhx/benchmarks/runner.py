"""Running benchmark tasks: each run gets a fresh environment, a fresh temporary project, the real
executor (with a deterministic approver), the agent loop and an independent evaluator.

    environment (simulated web / desktop / Android, a workspace, or the real browser)
      → App on a temporary project → ActionExecutor (risk · policy · approval · audit)
      → AgentLoop (planner → worker → observer → verifier → reflector)
      → trajectory → metrics + the benchmark's own evaluation of the final state

Approvals are answered by :class:`BenchmarkApprover`: actions up to the task's ``risk_level``
are approved, and anything riskier is declined, the way a careful person would. Safety tasks
use this to check that a declined action is never reported as done.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from highhx.actions import events as ev
from highhx.benchmarks.model import RISK_LEVELS, BenchmarkResult, BenchmarkSuite, BenchmarkTask, RunMetrics
from highhx.computer.session import ComputerSession
from highhx.core.events import new_id
from highhx.trajectories.store import Trajectory

SUITES_DIR = Path(__file__).parent / "suites"
RECOVERY_DECISIONS = frozenset({"retry", "reobserve", "scroll", "replan"})
RISK_ORDER = {name: i for i, name in enumerate(RISK_LEVELS)}
ENGINE_LEVELS = {"normal": "low", "dangerous": "high"}
"""The engine's four-level labels on the catalog's five-level scale (actions.policy.Risk.parse)."""


# ------------------------------------------------------------------ approvals
@dataclass
class BenchmarkApprover:
    """A deterministic person: approves up to ``max_risk``, declines above it."""

    max_risk: str = "medium"
    interactive: bool = True
    asked: int = 0
    declined: int = 0

    def _ok(self, risk: str) -> bool:
        self.asked += 1
        level = ENGINE_LEVELS.get(risk.lower(), risk.lower())
        ok = RISK_ORDER.get(level, 4) <= RISK_ORDER.get(self.max_risk, 2)
        if not ok:
            self.declined += 1
        return ok

    def confirm_action(self, request: Any) -> bool:
        return self._ok(str(request.risk_name))

    def ask_permission(self, action: str, details: Any, *, allow_always: bool = True) -> str:
        return "yes" if self._ok("low") else "no"

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return self._ok("medium")

    def confirm_typed(self, message: str, expected: str) -> bool:
        return self._ok("critical")

    def notice(self, level: str, message: str) -> None:
        pass


# --------------------------------------------------------------- environments
class BenchmarkSession(ComputerSession):
    """A computer session whose browser, desktop and Android device are the benchmark's."""

    def __init__(self, *args: Any, web: Any = None, desktop: Any = None, android: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._web = web
        self._desktop_env = desktop
        self._android = android
        self._desktop_driver: Any = None

    @property
    def browser(self) -> Any:
        if self._web is not None:
            return self._web
        return ComputerSession.browser.fget(self)  # type: ignore[attr-defined]

    def driver(self) -> Any:
        if self._desktop_env is None:
            return super().driver()
        if self._desktop_driver is None:
            from highhx.automation.engine.bridge import AutomationBridge
            from highhx.computer.driver import HighhXDriver

            self._desktop_driver = HighhXDriver(AutomationBridge(self._desktop_env))
        return self._desktop_driver

    def android_client(self, serial: str | None = None, cancel: Any = None) -> Any:
        if self._android is None:
            return super().android_client(serial, cancel)
        from highhx.drivers.android.adb import AdbClient

        return AdbClient(adb="adb", serial=serial or None, runner=self._android, cancel=cancel)


DEFAULT_DESKTOP: dict[str, Any] = {
    "front": "Notes",
    "apps": {
        "Notes": {
            "running": True,
            "saved": False,
            "window": {"id": 11, "pid": 110, "app": "Notes", "title": "Untitled", "x": 0, "y": 25, "width": 800, "height": 600},
            "menus": {"File": ["New", "Save", "Close"], "Edit": ["Copy", "Paste"]},
            "elements": [
                {"role": "textbox", "name": "Title", "bounds": [40, 80, 400, 24]},
                {"role": "textbox", "name": "Body", "bounds": [40, 120, 700, 400]},
                {"role": "button", "name": "Increment", "bounds": [460, 80, 90, 24]},
                {"role": "checkbox", "name": "Pinned", "bounds": [560, 80, 80, 24]},
            ],
        }
    },
}


@dataclass
class Environment:
    kind: str
    project: Path
    web: Any = None
    desktop: Any = None
    android: Any = None

    def surface(self) -> str:
        return {"web": "browser", "browser": "browser", "desktop": "desktop", "android": "android"}.get(self.kind, "none")


def build_environment(task: BenchmarkTask, base: Path) -> Environment:
    project = base / "project"
    project.mkdir(parents=True)
    spec = task.environment
    for name, content in (spec.get("files") or {}).items():
        path = project / name
        if not path.resolve().is_relative_to(project.resolve()):
            raise ValueError(f"benchmark file {name!r} is outside the project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content))
    env = Environment(task.kind, project)
    if task.kind == "web":
        from highhx.benchmarks.environments.web import BASE, FakeWebApp

        env.web = FakeWebApp(variant=str(spec.get("variant") or ""), url=str(spec.get("url") or f"{BASE}/"))
        env.web.hidden_until_scroll = bool(spec.get("hidden_until_scroll"))
        env.web.state.update(spec.get("state") or {})
    elif task.kind == "desktop":
        from highhx.benchmarks.environments.desktop import SimulatedDesktop

        env.desktop = SimulatedDesktop(spec.get("setup") or DEFAULT_DESKTOP)
    elif task.kind == "android":
        from highhx.benchmarks.environments.android import FakeDevice

        device = FakeDevice()
        for key in ("focused_app", "field_value", "duplicate_delete"):
            if key in spec:
                setattr(device, key, spec[key])
        env.android = device
    return env


# --------------------------------------------------------------- evaluation
def evaluate(task: BenchmarkTask, env: Environment, executor: Any) -> tuple[bool, list[str]]:
    """The benchmark's own check of the final state (independent of what the agent says)."""
    if not task.evaluate:
        if task.success is None:
            return True, ["no criteria"]
        from highhx.verification.declarative import Verdict, VerificationContext, verify

        state = None
        if env.surface() != "none":
            from highhx.actions.handlers.state import state_from_result

            result = executor.run("computer.state", {"surface": env.surface()})
            state = state_from_result(result) if result.ok else None
        report = verify(task.success, VerificationContext(after=state, root=env.project))
        return report.verdict == Verdict.SATISFIED, [report.result.detail]
    notes: list[str] = []
    passed = True
    for check in task.evaluate:
        ok, note = _check(check, env)
        passed = passed and ok
        notes.append(f"{'✓' if ok else '✗'} {note}")
    return passed, notes


def _check(check: dict[str, Any], env: Environment) -> tuple[bool, str]:
    (name, value), = check.items()
    if name == "state" and env.web is not None:
        wrong = {k: env.web.state.get(k) for k, v in value.items() if env.web.state.get(k) != v}
        return not wrong, f"state {value}" + (f" (is {wrong})" if wrong else "")
    if name == "url_contains" and env.web is not None:
        return str(value) in env.web.url, env.web.url
    if name == "desktop" and env.desktop is not None:
        from highhx.benchmarks.environments.desktop_evaluators import evaluate as desktop_evaluate

        result = desktop_evaluate("task", env.desktop, list(value))
        return result.passed, "; ".join(c[2] for c in result.checks)
    if name == "android" and env.android is not None:
        wrong = {k: getattr(env.android, k, None) for k, v in value.items() if getattr(env.android, k, None) != v}
        return not wrong, f"android {value}" + (f" (is {wrong})" if wrong else "")
    if name in ("file", "exit_code", "text"):
        from highhx.verification.declarative import VerificationContext
        from highhx.verification.declarative import evaluate as declarative

        checked = declarative({name: value}, VerificationContext(root=env.project))
        return checked.satisfied, checked.detail
    return False, f"unknown or inapplicable check {name!r} for a {env.kind} environment"


# ------------------------------------------------------------------- metrics
def _intent(step: Any) -> str:
    return str((step.action.get("step") or {}).get("intent") or step.intent)


def run_metrics(task: BenchmarkTask, trajectory: Trajectory, goal_achieved: bool, *, benchmark_id: str, run: int, provider: str, runtime: str) -> RunMetrics:
    claimed = trajectory.status == "completed"
    success = (goal_achieved and claimed) if task.expect_completion else (not goal_achieved and not claimed)
    final: dict[str, Any] = {}
    first: dict[str, Any] = {}
    for step in trajectory.steps:
        if step.grounding:
            first.setdefault(_intent(step), step)
            final[_intent(step)] = step
    graded = []
    healing_needed = healed = 0
    for intent, step in final.items():
        candidate = (step.grounding or {}).get("candidate") or {}
        element = candidate.get("element") or {}
        label = str(((step.grounding or {}).get("target") or {}).get("semantic", {}).get("label") or (step.action.get("target") or {}).get("label") or "")
        expected = task.ground_truth.get(label) or task.ground_truth.get(str((first[intent].action.get("target") or {}).get("label") or ""))
        if expected is not None:
            graded.append(element.get("name") == expected)
        else:
            graded.append(bool(candidate) and step.outcome == "success")
        attempts = (step.grounding or {}).get("attempts") or []  # the grounding that decided the step
        if attempts and attempts[0].get("result") == "failed":  # the primary selector no longer matched
            healing_needed += 1
            if candidate and candidate.get("strategy") != attempts[0].get("strategy") and step.outcome == "success":
                healed += 1
    episodes = recovered = 0
    for index, step in enumerate(trajectory.steps):
        if (step.reflection or {}).get("decision") in RECOVERY_DECISIONS:
            episodes += 1
            intent = _intent(step)
            if any(_intent(later) == intent and later.outcome == "success" for later in trajectory.steps[index + 1 :]):
                recovered += 1
    m = trajectory.metrics
    return RunMetrics(
        benchmark_id=benchmark_id,
        task_id=task.id,
        run=run,
        environment=task.kind,
        runtime=runtime,
        provider=provider,
        success=success,
        grounding_accuracy=round(sum(graded) / len(graded), 4) if graded else None,
        selector_healing_rate=round(healed / healing_needed, 4) if healing_needed else None,
        recovery_success_rate=round(recovered / episodes, 4) if episodes else None,
        verification_accuracy=1.0 if claimed == goal_achieved else 0.0,
        average_retries=float(int(m.get("recoveries") or 0) + int(m.get("replans") or 0)),
        completion_time=round(float(m.get("seconds") or 0.0), 3),
        token_cost=int(m.get("tokens_in") or 0) + int(m.get("tokens_out") or 0),
        tool_cost=int(m.get("actions") or 0) + int(m.get("observations") or 0),
        cost_usd=m.get("cost"),
        status=trajectory.status,
        summary=trajectory.summary,
        trajectory_id=trajectory.id,
    )


# -------------------------------------------------------------------- runner
ModelFactory = Callable[[Any], Any]


class BenchmarkRunner:
    def __init__(
        self,
        *,
        runs: int = 1,
        model_factory: ModelFactory | None = None,
        keep: Path | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.runs = runs
        self.model_factory = model_factory
        """Builds a LanguageModel for ``planner: {kind: model}`` tasks (None: those tasks are skipped)."""
        self.keep = keep
        """Where to keep each run's trajectory (None: discarded with the temporary project)."""
        self.on_event = on_event

    def _emit(self, name: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(name, data)

    def run_suite(self, suite: BenchmarkSuite) -> BenchmarkResult:
        benchmark_id = new_id("bench")
        provider = "model" if self.model_factory else "scripted"
        result = BenchmarkResult(benchmark_id, suite.name, time.time(), [], provider, "local")
        self._emit(ev.BENCHMARK_STARTED, benchmark=benchmark_id, suite=suite.name, tasks=len(suite.tasks), runs=self.runs)
        for task in suite.tasks:
            if task.planner.get("kind") == "model" and self.model_factory is None:
                self._emit(ev.BENCHMARK_TASK, benchmark=benchmark_id, task=task.id, skipped="needs a model (--model)")
                continue
            for run in range(1, self.runs + 1):
                metrics = self.run_task(task, benchmark_id=benchmark_id, run=run, provider=provider)
                result.runs.append(metrics)
                self._emit(ev.BENCHMARK_TASK, benchmark=benchmark_id, task=task.id, run=run, success=metrics.success, status=metrics.status, seconds=metrics.completion_time)
        self._emit(ev.BENCHMARK_COMPLETED, benchmark=benchmark_id, suite=suite.name, runs=len(result.runs))
        return result

    def run_task(self, task: BenchmarkTask, *, benchmark_id: str = "", run: int = 1, provider: str = "scripted") -> RunMetrics:
        from highhx.actions.executor import ActionExecutor
        from highhx.agent.loop import AgentLoop, AgentTask
        from highhx.commands import App
        from highhx.core.context import Options
        from highhx.safety.actions import Actor
        from highhx.safety.gate import ActionGate, ApprovalMode
        from highhx.trajectories import TrajectoryStore

        base = Path(tempfile.mkdtemp(prefix="highhx-bench-"))
        app = None
        executor = None
        holder: dict[str, Any] = {}
        try:
            env = build_environment(task, base)
            app = App(Options(interactive=False), cwd=env.project)
            approver = BenchmarkApprover(task.risk_level)
            model_planned = task.planner.get("kind") == "model"
            actor = Actor.AGENT if model_planned else Actor.USER
            gate = ActionGate(app.engine, approver, source="benchmark", mode=ApprovalMode.ASK)

            def session() -> Any:
                # every run gets its own session; a real browser runs on a throwaway profile in the
                # run's directory (never the person's HighhX browser) and is stopped afterwards
                if "s" not in holder:
                    holder["s"] = BenchmarkSession(gate, actor=actor, tool="benchmark", web=env.web, desktop=env.desktop, android=env.android, state_dir=base / "computer")
                return holder["s"]

            executor = ActionExecutor(app, gate, actor=actor, computer=session, sleep=lambda _s: None)
            planner = self._planner(task, app, executor)
            store = TrajectoryStore(self.keep or base / "trajectories", redactor=app.redactor)
            surface = task.surface or env.surface()
            agent_task = AgentTask(
                task.description,
                surface=surface,
                success=task.success,
                timeout=task.timeout,
                allowed=tuple(task.allowed_tools),
                settle=0.0 if task.kind != "browser" else 0.4,
            )
            simulated = task.kind in ("web", "desktop", "android", "workspace")
            loop = AgentLoop(executor, planner, store=store, memory=False, agent="benchmark", sleep=(lambda _s: None) if simulated else time.sleep)
            outcome = loop.run(agent_task)
            goal, _notes = evaluate(task, env, executor)
            return run_metrics(task, outcome.trajectory, goal, benchmark_id=benchmark_id, run=run, provider=provider, runtime="local")
        finally:
            if executor is not None:
                executor.close()
            opened = holder.get("s")
            if opened is not None:
                if task.kind == "browser" and opened._browser is not None:
                    opened._browser.stop()
                opened.close()
            if app is not None:
                app.close()
            shutil.rmtree(base, ignore_errors=True)

    def _planner(self, task: BenchmarkTask, app: Any, executor: Any) -> Any:
        from highhx.agent.loop import ModelPlanner, ResolverPlanner, ScriptedPlanner

        kind = task.planner.get("kind", "scripted")
        if kind == "scripted":
            return ScriptedPlanner(task.planner.get("steps") or [])
        if kind == "resolver":
            return ResolverPlanner(app, task.description, executor=executor)
        assert self.model_factory is not None
        return ModelPlanner(self.model_factory(app), executor.catalog)


# --------------------------------------------------------------------- suites
def builtin_suites() -> dict[str, Path]:
    return {p.stem: p for p in sorted(SUITES_DIR.glob("*.yaml"))}


def load_suite(name_or_path: str) -> BenchmarkSuite:
    path = Path(name_or_path)
    if path.suffix in (".yaml", ".yml") and path.is_file():
        return BenchmarkSuite.load(path)
    suites = builtin_suites()
    if name_or_path not in suites:
        from highhx.core.errors import NotFoundError

        raise NotFoundError(f"No benchmark suite {name_or_path!r}.", hint=f"Built in: {', '.join(suites)}; or pass a YAML file.")
    return BenchmarkSuite.load(suites[name_or_path])


def task_count(path: Path) -> int:
    data = yaml.safe_load(path.read_text()) or {}
    return len(data.get("tasks") or [])
