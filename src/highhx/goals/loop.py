"""The goal loop: observe → decide → validate → execute → verify → recover, until done.

    TASK      the goal
    PLAN      the IR's steps, or "discovered from the page"
    loop:
      OBSERVE   the page as it is now (URL, title, tab, accessibility tree, text)
      decide    the planner proposes one action (validated; a planner decision may end the task)
      police    it must not have failed on this very page already, must stay on the site when
                asked, must not exceed its repeat limit
      ACTION    executed through the computer runtime (safety policy, approval, audit)
      RESULT / VERIFY
      RECOVERY  on failure: look again and decide — retry only what cannot happen twice, reveal
                a missing element by scrolling, reload a stale page, or hand the problem to the
                planner (replanning); denials and cancellations end the task at once
    FINAL     completed, failed (with the reason), needs the user, or cancelled

Bounded everywhere: steps, failures, recoveries per step, repeats of one action, wall-clock
time, and a no-progress detector — the loop cannot spin.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from highhx.computer.browser import NavigationError
from highhx.computer.model import Observation
from highhx.computer.runtime import InvalidActionError
from highhx.core.errors import (
    ApprovalDeniedError,
    HighhXError,
    IntegrationError,
    ModelProviderError,
    NotFoundError,
    OperationCancelledError,
    OutcomeUnknownError,
    PolicyViolationError,
    UsageError,
    ValidationError,
)
from highhx.goals import conditions
from highhx.goals.executor import GoalExecutor, StepResult
from highhx.goals.ir import Action, Condition, TaskIR
from highhx.goals.log import TaskLog
from highhx.goals.planner import Invalid, ModelPlanner, Planner, ScriptedPlanner
from highhx.goals.state import (
    CANCELLED,
    COMPLETED,
    FAILED,
    NEEDS_USER,
    PageSummary,
    StepRecord,
    TaskState,
    page_key,
)

MAX_SAME_ACTION = 3
"""One action (same primitive, target and value) runs at most this often in a task."""
MAX_RECOVERIES_PER_STEP = 2
MAX_INVALID_PROPOSALS = 3
NO_PROGRESS_WINDOW = 6
"""This many failed steps in a row on one unchanged page: stop — nothing is changing."""
SUCCESS_WAIT = 5.0
"""How long the success conditions may take to become true after the last step."""

# failure kinds → what may be done about them
NOT_EXECUTED = "not_executed"  # the target was missing / the action was refused as invalid: nothing happened
UNKNOWN = "unknown_outcome"  # it may have happened: never repeated
UNVERIFIED = "unverified"  # it ran; the expected result did not appear
CONNECTION = "connection"  # the browser / page failed and the runtime could not recover it in time
DENIED = "denied"  # the safety policy or the person said no: final
NAVIGATION = "navigation"  # the site answered with an error (the runtime already retried transient ones)


@dataclass
class _Stop(Exception):
    status: str
    reason: str


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


class GoalLoop:
    def __init__(
        self,
        executor: GoalExecutor,
        planner: Planner,
        log: TaskLog,
        *,
        replanner: ModelPlanner | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.log_dir = log_dir
        """Where each task's log is written (``<task id>.jsonl``)."""
        self.executor = executor
        self.planner = planner
        self.log = log
        self.replanner = replanner
        self.cancel = executor.runtime.cancel
        self._health: tuple[int, str] = (0, "")

    # ---------------------------------------------------------------- helpers
    def check(self, condition: Condition, observation: Observation) -> list[str]:
        return conditions.check(condition, observation, self.executor.runtime)

    def _observe(self, state: TaskState) -> Observation:
        observation = self.executor.observe()
        self._runtime_recoveries(state)
        state.observed(observation, self.executor.tab())
        where = observation.title or observation.application
        self.log.add(
            "OBSERVE",
            f"{where} <{observation.url or '-'}> · {len(observation.elements)} elements",
            url=observation.url,
            title=observation.title,
            tab=state.tab,
            elements=len(observation.elements),
        )
        return observation

    def _budget(self, state: TaskState) -> None:
        limits = state.task.constraints
        if self.cancel.cancelled:
            raise _Stop(CANCELLED, "cancelled")
        if state.elapsed > limits.timeout:
            raise _Stop(FAILED, f"timed out after {limits.timeout:.0f}s")
        if len(state.steps) >= limits.max_steps:
            raise _Stop(FAILED, f"step limit reached ({limits.max_steps} actions)")
        if state.failures > limits.max_failures:
            raise _Stop(FAILED, f"too many failures ({state.failures})")
        recent = state.steps[-NO_PROGRESS_WINDOW:]
        if (
            len(recent) == NO_PROGRESS_WINDOW
            and not any(r.ok for r in recent)
            and len({(r.after.url if r.after else "") for r in recent}) == 1
        ):
            raise _Stop(FAILED, f"no progress: the last {NO_PROGRESS_WINDOW} actions failed on the same page")

    def _police(self, state: TaskState, action: Action) -> str | None:
        """Why the loop refuses a (valid) proposal, or None."""
        why = state.already_failed(action)
        if why is not None:
            return f"{action.call()} already failed on this page ({why}); it is not repeated"
        if state.attempts.get(action.signature(), 0) >= MAX_SAME_ACTION:
            return f"{action.call()} already ran {MAX_SAME_ACTION} times in this task"
        if state.task.constraints.stay_on_site and action.primitive in ("navigate", "new_tab") and action.value:
            start = state.task.context.get("start_url") or (state.observations[0].url if state.observations else "")
            if start and _host(start) and _host(action.value) != _host(start):
                return f"{action.value} leaves {_host(start)} (the task must stay on the site)"
        return None

    # ------------------------------------------------------------------- run
    def run(self, task: TaskIR) -> TaskState:
        state = TaskState(task, planner=self.planner.name)
        self._health = self.executor.health()
        if self.log_dir is not None:
            self.log.file = self.log_dir / f"{state.id}.jsonl"
        self.log.add("TASK", task.goal, task=state.id)
        if isinstance(self.planner, ScriptedPlanner) and task.steps:
            self.log.add(
                "PLAN",
                f"{_count(task)} ({self.planner.name}): {self.planner.describe()}",
                steps=task.to_dict()["steps"],
            )
        else:
            self.log.add("PLAN", "discovered from the page, one action at a time", planner=self.planner.name)
        if task.success_conditions:
            self.log.add("PLAN", "done when " + "; ".join(c.describe() for c in task.success_conditions))
        try:
            observation = self._observe(state)
            while True:
                self._budget(state)
                for condition in task.failure_conditions:
                    if not self.check(condition, observation):
                        raise _Stop(FAILED, f"failure condition met: {condition.describe()}")
                decision = self._decide(state, observation)
                if decision is None:
                    continue
                if decision.control:
                    observation = self._control(state, decision, observation)
                    continue
                refusal = self._police(state, decision)
                if refusal is not None:
                    observation = self._refuse(state, decision, refusal, observation)
                    continue
                observation = self._step(state, decision, observation)
                if (
                    task.success_conditions
                    and state.steps
                    and state.steps[-1].ok
                    and self._succeeded(state, observation, wait=0.0, quiet=True)
                ):
                    raise _Stop(COMPLETED, "the success conditions hold")
        except _Stop as stop:
            state.finish(stop.status, stop.reason)
        except OperationCancelledError:
            state.finish(CANCELLED, "cancelled")
        except (ApprovalDeniedError, PolicyViolationError) as exc:
            state.finish(FAILED, f"not allowed: {exc.message}")
        except ModelProviderError as exc:
            state.finish(FAILED, f"the planner model failed: {exc.message}")
        except HighhXError as exc:
            state.finish(FAILED, exc.message)
        self._final(state)
        return state

    def _decide(self, state: TaskState, observation: Observation) -> Action | None:
        decision = self.planner.next(state, observation)
        if isinstance(decision, Invalid):
            state.invalid_proposals += 1
            state.failures += 1
            self.log.add(
                "RECOVERY",
                f"the planner proposed an invalid action ({'; '.join(decision.errors[:3])}); it was not executed",
                ok=False,
                errors=decision.errors,
            )
            if state.invalid_proposals >= MAX_INVALID_PROPOSALS:
                raise _Stop(FAILED, f"the planner proposed {MAX_INVALID_PROPOSALS} invalid actions")
            return None
        return decision

    def _control(self, state: TaskState, decision: Action, observation: Observation) -> Observation:
        if decision.primitive == "fail":
            raise _Stop(FAILED, str(decision.value or decision.reason))
        if decision.primitive == "ask_user":
            raise _Stop(NEEDS_USER, str(decision.value or decision.reason))
        # done: the planner's word is checked against the goal's success conditions
        if not state.task.success_conditions or self._succeeded(
            state, observation, wait=min(SUCCESS_WAIT, state.task.constraints.action_timeout)
        ):
            raise _Stop(COMPLETED, decision.reason)
        state.failures += 1
        reason = "the plan finished but the goal is not reached"
        return self._replan(state, observation, reason, decision)

    def _succeeded(self, state: TaskState, observation: Observation, *, wait: float, quiet: bool = False) -> bool:
        problems: list[str] = []
        for condition in state.task.success_conditions:
            found, observation = (
                self.executor.holds(condition, wait) if wait else (self.check(condition, observation), observation)
            )
            problems += found
        if problems and quiet:
            return False
        for condition in state.task.success_conditions:
            if not problems:
                self.log.add("VERIFY", f"goal: {condition.describe()}", ok=True, goal=True)
        for problem in problems:
            self.log.add("VERIFY", f"goal: {problem}", ok=False, goal=True)
        return not problems

    def _refuse(self, state: TaskState, action: Action, reason: str, observation: Observation) -> Observation:
        state.failures += 1
        self.log.add("RECOVERY", reason, ok=False)
        if isinstance(self.planner, ModelPlanner):
            self.planner.rejected(reason)
            return observation
        return self._replan(state, observation, reason, action)

    def _replan(self, state: TaskState, observation: Observation, reason: str, action: Action) -> Observation:
        """The deterministic plan cannot go on: hand the task to the model planner, or stop."""
        if isinstance(self.planner, ModelPlanner):
            self.planner.rejected(reason)
            return observation
        if self.replanner is None or not state.task.allow_replanning:
            raise _Stop(FAILED, f"{action.call()}: {reason}")
        state.replans += 1
        self.planner = self.replanner
        state.planner = self.replanner.name
        self.replanner.rejected(f"{action.call()} failed: {reason}")
        self.log.add("PLAN", f"replanning: {reason} — the planner continues from the page", replan=state.replans)
        return self._observe(state)

    # ------------------------------------------------------------------ step
    def _execute(self, state: TaskState, action: Action, observation: Observation) -> tuple[StepRecord, str]:
        """Run ``action`` once; the record and, when it failed, the failure kind."""
        timeout = state.task.constraints.action_timeout
        signature = action.signature()
        state.attempts[signature] = state.attempts.get(signature, 0) + 1
        self.log.add("ACTION", action.call(), reason=action.reason, action=action.to_dict())
        started = time.monotonic()
        kind = ""
        try:
            result = self.executor.run(action, timeout)
        except OperationCancelledError:
            raise
        except (ApprovalDeniedError, PolicyViolationError) as exc:
            result, kind = StepResult(False, False, exc.message, [exc.message]), DENIED
        except (NotFoundError, InvalidActionError, UsageError, ValidationError) as exc:
            detail = exc.message + (f" {exc.hint}" if exc.hint else "")
            result, kind = StepResult(False, False, exc.message, [detail]), NOT_EXECUTED
        except OutcomeUnknownError as exc:
            result, kind = StepResult(False, None, exc.message, [exc.message, *exc.details[:2]]), UNKNOWN
        except NavigationError as exc:
            detail = exc.message + (f" ({exc.hint})" if exc.hint else "")
            result, kind = StepResult(False, False, exc.message, [detail]), NAVIGATION
        except IntegrationError as exc:
            result, kind = StepResult(False, False, exc.message, [exc.message, *exc.details[:2]]), CONNECTION
        if not kind and not result.ok:
            kind = UNVERIFIED
        self._runtime_recoveries(state, action.primitive)
        record = StepRecord(
            len(state.steps) + 1,
            action,
            result.ok,
            result.verified,
            result.summary,
            result.problems,
            before=PageSummary.of(observation, state.tab),
            after=PageSummary.of(result.observation or observation, self.executor.tab() or state.tab),
            output=result.output,
            seconds=time.monotonic() - started,
        )
        state.steps.append(record)
        if action.primitive == "read" and result.output:
            state.extracted[f"step_{record.number}"] = (
                result.output.get("matches") or result.output.get("text", "")[:500]
            )
        self.log.add("RESULT", f"{result.summary} ({record.seconds:.1f}s)", ok=result.ok, step=record.number)
        if result.ok:
            detail = action.expected_result or (action.expect.describe() if action.expect else "")
            if result.verified:
                self.log.add("VERIFY", detail or "verified", ok=True, step=record.number)
            elif result.verified is None:
                self.log.add("VERIFY", "not verifiable here" + (f" ({detail})" if detail else ""), step=record.number)
        else:
            for problem in result.problems[:3] or [result.summary]:
                self.log.add("VERIFY", problem, ok=False, step=record.number)
        return record, kind

    def _runtime_recoveries(self, state: TaskState, primitive: str = "observe") -> None:
        """Say what the browser runtime recovered from underneath the task (it does so by itself):
        compared with the last look, the connection was rebuilt or the working tab replaced."""
        reconnects, tab = self.executor.health()
        before_reconnects, before_tab = self._health
        self._health = (reconnects, tab)
        if reconnects > before_reconnects:
            state.recoveries += 1
            self.log.add(
                "RECOVERY",
                "the browser connection was lost; HighhX reconnected (restarting the browser if it had exited)",
                ok=True,
                reconnects=reconnects,
            )
        moved_on_purpose = primitive in ("new_tab", "close_tab", "switch_tab") or (
            primitive == "navigate" and before_tab in self._open_tabs()  # opened beside a page that stays open
        )
        if before_tab and tab and tab != before_tab and not moved_on_purpose:
            state.recoveries += 1
            self.log.add(
                "RECOVERY", "the working tab was closed or replaced; continuing in another tab", ok=True, tab=tab
            )

    def _open_tabs(self) -> set[str]:
        tabs = getattr(getattr(self.executor.runtime.provider, "tabs", None), "tabs", None)
        return set(tabs) if isinstance(tabs, dict) else set()

    def _step(self, state: TaskState, action: Action, observation: Observation) -> Observation:
        record, kind = self._execute(state, action, observation)
        if record.ok:
            self.planner.advance()
            return self._after(state)
        state.failures += 1
        state.failed[state.key(action, observation)] = (record.problems or [record.summary])[0][:160]
        return self._recover(state, action, record, kind)

    def _after(self, state: TaskState) -> Observation:
        observation = self.executor.runtime.observation or self.executor.observe()
        state.observed(observation, self.executor.tab())
        return observation

    # -------------------------------------------------------------- recovery
    def _recover(self, state: TaskState, action: Action, record: StepRecord, kind: str) -> Observation:
        if kind == DENIED:
            raise _Stop(FAILED, f"not allowed: {record.summary}")
        for attempt in range(1, MAX_RECOVERIES_PER_STEP + 1):
            self._budget(state)
            state.recoveries += 1
            try:
                observation = self._observe(state)  # always look again first: the page decides
            except OperationCancelledError:
                raise
            except HighhXError as exc:
                record.recovery = "the browser did not answer"
                self.log.add("RECOVERY", f"the browser did not answer ({exc.message}); trying again", ok=False)
                if self.cancel.wait(0.5 * attempt):
                    raise OperationCancelledError("cancelled") from None
                continue
            retry = self._may_retry(state, action, kind, attempt, observation)
            if retry is None:
                record.recovery = "handed to the planner"
                return self._replan(
                    state, observation, record.problems[0] if record.problems else record.summary, action
                )
            record.recovery = retry
            self.log.add("RECOVERY", retry)
            current = self.executor.runtime.observation  # the recovery may have changed the page (scroll, reload)
            if current is not None and current is not observation:
                observation = current
                state.observed(observation, self.executor.tab())
            again, kind = self._execute(state, action, observation)
            if again.ok:
                self.planner.advance()
                return self._after(state)
            state.failures += 1
            state.failed[state.key(action, observation)] = (again.problems or [again.summary])[0][:160]
            record = again
            if kind == DENIED:
                raise _Stop(FAILED, f"not allowed: {again.summary}")
        observation = self._observe(state)
        cause = record.problems[0] if record.problems else record.summary
        return self._replan(
            state, observation, f"{cause} (still failing after {MAX_RECOVERIES_PER_STEP} recoveries)", action
        )

    def _may_retry(
        self, state: TaskState, action: Action, kind: str, attempt: int, observation: Observation
    ) -> str | None:
        """How to try ``action`` again now — or None: it must not be repeated (the planner decides)."""
        if state.attempts.get(action.signature(), 0) >= MAX_SAME_ACTION:
            return None
        if kind == UNKNOWN:
            return None  # it may have happened: never twice
        if kind == NOT_EXECUTED and action.target:
            if attempt == 1:
                self.log.add("RECOVERY", f"{action.target!r} is not on the page; scrolling to look for it")
                try:
                    self.executor.run(Action("browser.scroll", "reveal the target", value="down"), 5.0)
                except HighhXError:
                    return None
                changed = page_key(self.executor.runtime.observation) != page_key(observation)
                return "retrying on the scrolled page" if changed else None
            return None
        if kind == CONNECTION:
            if action.idempotent:
                return "the browser recovered; retrying (it cannot happen twice)"
            return None
        if kind == UNVERIFIED:
            if action.idempotent and attempt == 1 and action.primitive not in ("verify", "find", "read"):
                try:
                    self.executor.run(Action("browser.recover", "reload a page that did not settle"), 15.0)
                except HighhXError:
                    return None
                return "reloaded the page; retrying"
            return None
        return None

    # ------------------------------------------------------------------ end
    def _final(self, state: TaskState) -> None:
        steps = len(state.steps)
        tail = f"({steps} step{'s' if steps != 1 else ''}, {state.elapsed:.1f}s)"
        if state.status == COMPLETED:
            self.log.add("FINAL", f"Task completed {tail}", ok=True, status=state.status, reason=state.reason)
        elif state.status == NEEDS_USER:
            self.log.add("FINAL", f"Needs you: {state.reason} {tail}", status=state.status, reason=state.reason)
        elif state.status == CANCELLED:
            self.log.add("FINAL", f"Cancelled {tail}", ok=False, status=state.status, reason=state.reason)
        else:
            self.log.add(
                "FINAL", f"Task failed: {state.reason} {tail}", ok=False, status=state.status, reason=state.reason
            )


def _count(task: TaskIR) -> str:
    n = len(task.steps)
    return f"{n} step{'s' if n != 1 else ''}"
