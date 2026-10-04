"""The agent loop: Planner → Worker → Observer → Verifier → Reflector, until done.

    TASK ── route the surface ── outline ── memory (lessons from similar tasks)
      loop
        OBSERVE    computer.state (an audited action), structure first
        PLAN       the planner proposes one step (data)
        WORK       ground the target (escalating DOM → AX → text → OCR → vision → coordinates)
                   and submit ActionRequests to the executor (risk · policy · approval · audit)
        VERIFY     declarative check on a fresh observation. UNKNOWN → observe again
        REFLECT    continue · retry · look again · scroll · re-plan · stop · ask the person
        RECORD     the trajectory step, then a checkpoint
      DONE         the task's success check on a fresh observation (never assumed)

Bounded everywhere: steps, failures, re-plans, recoveries (per step and total), wall-clock time,
and a no-progress detector. Interrupted tasks keep their checkpoint (``highhx agent --resume
<task id>``). Every event carries the task's trace id, task id and the agent's name, so the TUI,
traces and benchmarks see the same run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions import events as ev
from highhx.actions.executor import UnknownActionError
from highhx.actions.protocol import Outcome
from highhx.agent.loop.model import AgentTask, Decision, LoopResult, PlanItem, Status, StepIntent
from highhx.agent.loop.observer import AgentObserver, ObservationError
from highhx.agent.loop.planner import AgentPlanner, ScriptedPlanner
from highhx.agent.loop.recovery import RecoveryManager
from highhx.agent.loop.reflector import AgentReflector, Reflection
from highhx.agent.loop.routing import ToolRouter
from highhx.agent.loop.verifier import AgentVerifier, StepVerdict
from highhx.agent.loop.worker import AgentWorker, GroundingFailed, UnsupportedStep, WorkResult
from highhx.core.errors import HighhXError, OperationCancelledError, ValidationError
from highhx.core.events import trace_context
from highhx.perception.challenges import detect as detect_challenge
from highhx.trajectories.store import Trajectory, TrajectoryStep
from highhx.verification.declarative import Verdict, VerificationContext, verify

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.grounding import HybridGrounder
    from highhx.observability.tasktrace import TraceStore
    from highhx.perception.state import ComputerState
    from highhx.trajectories.store import TrajectoryStore

NO_PROGRESS = 3
"""The same screen this many times in a row after failed steps means no progress."""


@dataclass
class Counters:
    steps: int = 0
    actions: int = 0
    failures: int = 0
    replans: int = 0
    recoveries: int = 0
    observations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = None
    grounding: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Counters:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _redacted_step(step: StepIntent, *, secret: bool = False) -> dict[str, Any]:
    """The semantic step (verb, target, parameters) as recorded. Typed text is kept for replay
    unless it went into a secret field (password, card, one-time code). Known secret values are
    also removed by the redactor when the trajectory is saved."""
    data = step.to_dict()
    params = dict(data["parameters"])
    if "text" in params and (secret or str(step.target.get("role", "")).lower() in ("password", "secret")):
        params["text"] = f"<{len(str(params['text']))} characters>"
    data["parameters"] = params
    return data


def _observation(state: ComputerState | None) -> dict[str, Any]:
    if state is None:
        return {}
    return {
        "fingerprint": state.fingerprint(),
        "surface": state.surface,
        "url": state.url,
        "app": state.active_app,
        "title": state.title,
        "elements": len(state.elements),
        "screenshot": state.screenshot.to_dict() if state.screenshot else None,
    }


class AgentLoop:
    def __init__(
        self,
        executor: ActionExecutor,
        planner: AgentPlanner,
        *,
        store: TrajectoryStore | None = None,
        grounder: HybridGrounder | None = None,
        router: ToolRouter | None = None,
        agent: str = "agent",
        session_id: str | None = None,
        memory: bool = True,
        traces: TraceStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executor = executor
        self.traces = traces
        self.planner = planner
        self.store = store
        self.grounder = grounder
        self.router = router or ToolRouter()
        self.agent = agent
        self.session_id = session_id
        self.memory = memory
        self.sleep = sleep
        self.clock = clock
        self._secret_steps: set[str] = set()

    @property
    def events(self) -> Any:
        return self.executor.events

    # -------------------------------------------------------------------- entry
    def run(self, task: AgentTask, *, trajectory: Trajectory | None = None) -> LoopResult:
        resumed = trajectory is not None
        surface = task.surface if task.surface != "auto" else self.router.surface(task.goal)
        trajectory = trajectory or Trajectory(task.goal, surface, planner=self.planner.name, agent=self.agent)
        trajectory.surface = surface
        checkpoint = trajectory.metrics.get("checkpoint") or {}
        counters = Counters.from_dict(checkpoint.get("counters") or {})
        recorder = None
        if self.traces is not None:
            from highhx.observability.tasktrace import TaskTraceRecorder

            recorder = TaskTraceRecorder(self.events, self.traces, redactor=self.executor.app.redactor)
        try:
            return self._traced(task, surface, trajectory, counters, resumed)
        finally:
            if recorder is not None:
                recorder.save(trajectory.trace_id)
                recorder.recorder.close()

    def _traced(self, task: AgentTask, surface: str, trajectory: Trajectory, counters: Counters, resumed: bool) -> LoopResult:
        with trace_context(trace_id=trajectory.trace_id, task_id=trajectory.id, session_id=self.session_id, source=self.agent):
            self.events.emit(ev.AGENT_STARTED, task=task.goal, surface=surface, planner=self.planner.name, resumed=resumed, agent=self.agent)
            self.events.emit(ev.TASK_STARTED, task=task.goal, surface=surface, resumed=resumed)
            try:
                status, summary = self._run(task, surface, trajectory, counters, resumed)
            except (KeyboardInterrupt, OperationCancelledError):
                status, summary = Status.INTERRUPTED, "interrupted — resume with `highhx agent --resume " + trajectory.id + "`"
            trajectory.status = str(status)
            trajectory.summary = summary
            trajectory.ended = time.time()
            trajectory.metrics.update(self._metrics(counters, trajectory))
            self._checkpoint(task, trajectory, counters)
            self.events.emit(
                ev.TASK_COMPLETED if status == Status.COMPLETED else ev.TASK_FAILED,
                task=task.goal,
                status=str(status),
                summary=summary,
            )
            self.events.emit(
                ev.AGENT_COMPLETED,
                task=task.goal,
                status=str(status),
                summary=summary,
                steps=counters.steps,
                actions=counters.actions,
                agent=self.agent,
            )
        return LoopResult(status, summary, trajectory, trajectory.metrics)

    # ---------------------------------------------------------------------- run
    def _run(self, task: AgentTask, surface: str, trajectory: Trajectory, counters: Counters, resumed: bool) -> tuple[Status, str]:
        deadline = self.clock() + task.timeout
        cancel = self.executor.app.ctx.cancel
        observer = AgentObserver(self.executor, task, surface)
        worker = AgentWorker(self.executor, task, surface, grounder=self.grounder, memory=self.store if self.memory else None, trace_id=trajectory.trace_id)
        recovery = RecoveryManager(max_per_step=3, max_total=task.max_recoveries, deadline=deadline)
        recovery.total = counters.recoveries
        reflector = AgentReflector(recovery)
        verifier = AgentVerifier(lambda: self._observe(observer, counters), settle=task.settle, emit=self.events.emit, cancel=cancel, sleep=self.sleep)
        state = self._observe(observer, counters)
        if not resumed or not trajectory.plan:
            self.events.emit(ev.AGENT_PLANNING, task=task.goal, planner=self.planner.name)
            plan = self.planner.outline(task, state)
            trajectory.plan = [p.to_dict() for p in plan]
            self.events.emit(ev.PLAN_CREATED, steps=[p.title for p in plan], planner=self.planner.name)
        lessons = self.store.lessons(task.goal) if self.store is not None and self.memory else []
        feedback = ""
        same_screen = 0
        last_fingerprint = ""
        while True:
            if cancel.cancelled:
                return Status.CANCELLED, "cancelled"
            if self.clock() > deadline:
                return Status.FAILED, f"the task did not finish within {task.timeout:g}s"
            if counters.steps >= task.max_steps:
                return Status.FAILED, f"the task did not finish within {task.max_steps} steps"
            if counters.failures > task.max_failures:
                return Status.FAILED, f"{counters.failures} failed steps"
            fingerprint = state.fingerprint() if state is not None else ""
            same_screen = same_screen + 1 if fingerprint and fingerprint == last_fingerprint and counters.failures else 0
            last_fingerprint = fingerprint
            if same_screen >= NO_PROGRESS:
                return Status.FAILED, "no progress: the screen stopped changing while steps kept failing"
            challenge = detect_challenge(state)
            if challenge is not None:  # a person must answer it: never solved or clicked through
                reason = (
                    f"a human-verification challenge (CAPTCHA) is on the screen — {challenge.evidence}. "
                    f"Solve it yourself, then resume with `highhx agent --resume {trajectory.id}`"
                )
                self.events.emit(ev.AGENT_REFLECTION, decision="ask_user", reason=reason, challenge=challenge.kind)
                return Status.NEEDS_USER, reason
            try:
                decision = self.planner.next(task, state, trajectory.steps, feedback=feedback, lessons=lessons)
            except HighhXError as exc:  # a model that is down or refuses: the task stops, resumable
                return Status.FAILED, f"the planner failed: {exc.message} — resume with `highhx agent --resume {trajectory.id}`"
            feedback = ""
            self._usage(decision, counters)
            if decision.kind == "done":
                unconfirmed = sum(1 for s in trajectory.steps if (s.verification or {}).get("unobservable"))
                if unconfirmed and task.success is None:
                    return Status.NEEDS_USER, f"done, but {unconfirmed} step(s) could not be confirmed and the task has no success check"
                ok, why = self._finished(task, observer, counters)
                if ok is True:
                    return Status.COMPLETED, decision.summary or why
                if ok is None:
                    return Status.NEEDS_USER, f"the planner says it is done, but that could not be confirmed ({why})"
                counters.replans += 1
                if counters.replans > task.max_replans or isinstance(self.planner, ScriptedPlanner):
                    return Status.FAILED, f"not achieved: {why}"
                feedback = f"You said the task is done, but its success check failed: {why}"
                state = self._observe(observer, counters)
                continue
            if decision.kind == "ask_user":
                return Status.NEEDS_USER, decision.summary
            if decision.kind == "fail" or decision.step is None:
                return Status.FAILED, decision.summary or "the planner gave up"
            step = decision.step
            if step.parameters.get("__redacted__"):
                return Status.NEEDS_USER, f"the text for '{step.describe()}' went into a secret field and was not stored; provide it again"
            counters.steps += 1
            with trace_context(step_id=step.id):
                self._plan_progress(trajectory, step, "running")
                outcome, reflection, state = self._step(step, state, task, trajectory, counters, worker, observer, verifier, reflector)
                self._plan_progress(trajectory, step, "done" if outcome == Outcome.SUCCESS else "failed")
            self._checkpoint(task, trajectory, counters)
            if reflection.decision == "continue":
                continue
            if reflection.decision == "stop":
                return Status.FAILED, reflection.reason
            if reflection.decision == "ask_user":
                return Status.NEEDS_USER, reflection.reason
            counters.failures += 1
            if reflection.decision == "retry":
                counters.recoveries += 1
                self._rewind()
                if not isinstance(self.planner, ScriptedPlanner):
                    feedback = f"Try the last step ({step.describe()}) again: {reflection.reason}."
                continue
            counters.replans += 1
            if counters.replans > task.max_replans:
                return Status.FAILED, f"gave up after {task.max_replans} re-plans: {reflection.reason}"
            if isinstance(self.planner, ScriptedPlanner):
                self._rewind()  # a script re-tries its step from a fresh observation
            feedback = f"The last step ({step.describe()}) did not work: {reflection.reason}."
            state = self._observe(observer, counters)

    # --------------------------------------------------------------------- step
    def _step(
        self,
        step: StepIntent,
        state: ComputerState | None,
        task: AgentTask,
        trajectory: Trajectory,
        counters: Counters,
        worker: AgentWorker,
        observer: AgentObserver,
        verifier: AgentVerifier,
        reflector: AgentReflector,
    ) -> tuple[Outcome, Reflection, ComputerState | None]:
        while True:
            started = time.monotonic()
            try:
                work = worker.execute(step, state, escalate=observer.escalate if observer.active else None)
            except GroundingFailed as failure:
                attempts = [a.to_dict() for a in failure.result.attempts]
                reflection = reflector.after_grounding(step.id, failure.result.status, attempts, step.label)
                self._record(trajectory, step, state, None, None, reflection, failure.result.to_dict(), started, error=failure.result.explain())
                if reflection.decision in ("reobserve", "scroll"):
                    counters.recoveries += 1
                    self.events.emit(ev.RECOVERY_STARTED, step=step.id, kind=reflection.decision, reason=reflection.reason)
                    if reflection.decision == "scroll":
                        self._scroll(worker, state, observer)
                    else:
                        self.sleep(task.settle)
                    state = self._observe(observer, counters)
                    self.events.emit(ev.RECOVERY_COMPLETED, step=step.id, kind=reflection.decision)
                    continue
                return Outcome.FAILED, reflection, state
            except (UnsupportedStep, UnknownActionError, ValidationError) as exc:
                reason = getattr(exc, "message", str(exc))
                reflection = Reflection("replan", f"the step is not possible: {reason}")
                self._record(trajectory, step, state, None, None, reflection, None, started, error=reason)
                return Outcome.FAILED, reflection, state
            except ObservationError as exc:
                reflection = Reflection("stop" if exc.status in ("blocked", "denied") else "replan", f"observation failed: {exc}")
                self._record(trajectory, step, state, None, None, reflection, None, started, error=str(exc))
                return Outcome.FAILED, reflection, state
            self._protect_secret(step, work)
            counters.actions += len(work.responses)
            if work.grounding is not None and work.grounding.strategy:
                counters.grounding[work.grounding.strategy] = counters.grounding.get(work.grounding.strategy, 0) + 1
            verdict = verifier.verify(step, work, state)
            counters.observations += verdict.observations
            counters.outcomes[str(verdict.outcome)] = counters.outcomes.get(str(verdict.outcome), 0) + 1
            reflection = reflector.after_step(step.id, work.last, verdict.outcome, step.label, unobservable=verdict.unobservable)
            self.events.emit(ev.AGENT_REFLECTION, step=step.id, **reflection.to_dict())
            grounding = work.grounding.to_dict() if work.grounding else None
            if grounding is not None:
                grounding["target"] = work.recorded_target() or grounding["target"]
                self._healed(step, grounding, verdict.outcome)
            self._record(trajectory, step, state, work, verdict, reflection, grounding, started)
            after = verdict.after if verdict.after is not None else (state if not observer.active else self._observe(observer, counters))
            return verdict.outcome, reflection, after

    # ------------------------------------------------------------------ helpers
    def _observe(self, observer: AgentObserver, counters: Counters) -> ComputerState | None:
        if not observer.active:
            return None
        state = observer.observe()
        counters.observations += 1
        return state

    def _healed(self, step: StepIntent, grounding: dict[str, Any], outcome: Outcome) -> None:
        """A recorded target found by another representation than its first one: a healed selector."""
        attempts = grounding.get("attempts") or []
        recorded = bool(step.target.get("accessibility") or step.target.get("dom"))
        if not recorded or not attempts or attempts[0].get("result") != "failed" or outcome != Outcome.SUCCESS:
            return
        candidate = grounding.get("candidate") or {}
        fresh = grounding.get("target") or {}
        self.events.emit(
            ev.SELECTOR_HEALED,
            step=step.id,
            was=step.target.get("label"),
            now=fresh.get("label"),
            strategy=candidate.get("strategy"),
            confidence=candidate.get("score"),
            reason=f"{attempts[0].get('strategy')} no longer matched",
        )

    def _protect_secret(self, step: StepIntent, work: WorkResult) -> None:
        """Text typed into a secret field (password, card, one-time code) is registered with the
        redactor, so no log, audit row, trajectory or checkpoint keeps it."""
        candidate = work.grounding.candidate if work.grounding is not None else None
        text = step.parameters.get("text")
        if candidate is not None and candidate.element is not None and candidate.element.secret and text:
            self.executor.app.redactor.add([str(text)])
            self._secret_steps.add(step.id)

    def _scroll(self, worker: AgentWorker, state: ComputerState | None, observer: AgentObserver) -> None:
        try:
            worker.execute(StepIntent("scroll", parameters={"direction": "down"}, intent="reveal the target"), state)
        except HighhXError:
            pass  # scrolling is only an attempt to reveal the target; grounding decides next

    def _rewind(self) -> None:
        rewind = getattr(self.planner, "rewind", None)
        if callable(rewind):
            rewind()

    def _finished(self, task: AgentTask, observer: AgentObserver, counters: Counters) -> tuple[bool | None, str]:
        if task.success is None:
            return True, "done"
        state = self._observe(observer, counters)

        def again() -> ComputerState:
            fresh = self._observe(observer, counters)
            assert fresh is not None  # only used when the task has a screen
            return fresh

        ctx = VerificationContext(after=state, root=self.executor.app.root, observe=again if observer.active else None)
        self.events.emit(ev.VERIFICATION_STARTED, step="task", check=task.success)
        report = verify(task.success, ctx, timeout=3.0 if observer.active else 0.0, interval=task.settle, sleep=self.sleep)
        self.events.emit(ev.VERIFICATION_COMPLETED, step="task", verdict=str(report.verdict))
        if report.verdict == Verdict.SATISFIED:
            return True, "the task's success check holds"
        detail = "; ".join(c.detail for c in report.result.children if not c.satisfied) or report.result.detail
        return (None if report.verdict == Verdict.UNKNOWN else False), detail

    def _usage(self, decision: Decision, counters: Counters) -> None:
        reply = decision.reply
        if reply is None:
            return
        counters.tokens_in += reply.input_tokens
        counters.tokens_out += reply.output_tokens
        if reply.cost is not None:
            counters.cost = (counters.cost or 0.0) + reply.cost
        self.events.emit(ev.MODEL_USAGE, model=reply.model, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens, cost=reply.cost)

    def _plan_progress(self, trajectory: Trajectory, step: StepIntent, status: str) -> None:
        items = trajectory.plan
        if isinstance(self.planner, ScriptedPlanner):
            index = max(0, self.planner.cursor - 1)
            if index < len(items):
                items[index]["status"] = status
        elif status == "running":
            items.append(PlanItem(step.describe(), status).to_dict())
        elif items:
            items[-1]["status"] = status
        self.events.emit(ev.PLAN_UPDATED, items=[dict(i) for i in items], current=step.describe(), status=status)

    def _record(
        self,
        trajectory: Trajectory,
        step: StepIntent,
        state: ComputerState | None,
        work: WorkResult | None,
        verdict: StepVerdict | None,
        reflection: Reflection,
        grounding: dict[str, Any] | None,
        started: float,
        *,
        error: str = "",
    ) -> None:
        last = work.last if work is not None else None
        action = work.requests[-1].redacted() if work is not None and work.requests else {"action_type": step.action, "target": step.target, "parameters": step.parameters, "intent": step.describe()}
        candidate = work.grounding.candidate if work is not None and work.grounding is not None else None
        secret = bool(candidate is not None and candidate.element is not None and candidate.element.secret)
        action["step"] = _redacted_step(step, secret=secret)
        result = last.to_dict() if last is not None else {"outcome": "failed", "status": "not_run", "error": error}
        if verdict is not None:
            result["outcome"] = str(verdict.outcome)
        trajectory.add(
            TrajectoryStep(
                len(trajectory.steps) + 1,
                step.describe(),
                action,
                result,
                _observation(state),
                verdict.to_dict() if verdict is not None else None,
                reflection.to_dict(),
                grounding,
                time.time(),
                time.monotonic() - started,
            )
        )

    def _metrics(self, counters: Counters, trajectory: Trajectory) -> dict[str, Any]:
        return {**counters.to_dict(), "seconds": round((trajectory.ended or time.time()) - trajectory.started, 3)}

    def _checkpoint(self, task: AgentTask, trajectory: Trajectory, counters: Counters) -> None:
        planner_state = self.planner.state()
        for item in planner_state.get("steps") or []:
            if item.get("id") in self._secret_steps and "text" in (item.get("parameters") or {}):
                item["parameters"] = {**item["parameters"], "text": None, "__redacted__": True}
        trajectory.metrics["checkpoint"] = {
            "task": task.to_dict(),
            "planner": planner_state,
            "counters": counters.to_dict(),
            "surface": trajectory.surface,
        }
        if self.store is not None:
            self.store.save(trajectory)
            self.events.emit(ev.CHECKPOINT_CREATED, task_id=trajectory.id, steps=len(trajectory.steps), status=trajectory.status)


RESUMABLE = ("running", "interrupted", "needs_user", "failed", "cancelled")


def resume(
    executor: ActionExecutor,
    store: TrajectoryStore,
    task_id: str,
    *,
    planner: AgentPlanner | None = None,
    **loop_kwargs: Any,
) -> LoopResult:
    """Continue an interrupted task from its checkpoint: the same task, trace id, plan, history and
    counters. A scripted plan continues at its next step, and a model planner starts from the
    history so far."""
    from highhx.agent.loop.planner import planner_from_state

    trajectory = store.load(task_id)
    if trajectory.status not in RESUMABLE:
        from highhx.core.errors import UsageError

        raise UsageError(f"Task {trajectory.id} is {trajectory.status}; only unfinished tasks can be resumed.")
    checkpoint = trajectory.metrics.get("checkpoint")
    if not checkpoint:
        raise ValidationError(f"Task {trajectory.id} has no checkpoint.")
    task = AgentTask.from_dict(checkpoint["task"])
    task.surface = str(checkpoint.get("surface") or task.surface)
    planner = planner or planner_from_state(checkpoint["planner"], catalog=executor.catalog)
    trajectory.status = "running"
    with trace_context(trace_id=trajectory.trace_id, task_id=trajectory.id):
        executor.events.emit(ev.CHECKPOINT_RESUMED, task_id=trajectory.id, steps=len(trajectory.steps), cursor=checkpoint["planner"].get("cursor"))
    return AgentLoop(executor, planner, store=store, **loop_kwargs).run(task, trajectory=trajectory)
