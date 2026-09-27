"""Autonomous tasks (HighhX Pro): the agent works until HighhX has verified the result.

    "fix the login bug and make sure all tests pass"
      → definition of done, derived deterministically: [project.test]
      → attempt 1: the agent plans and works (its normal loop — tools, approvals, verification)
      → HighhX runs the checks itself, as actions (it never takes the model's word for "done")
      → failing checks and their output go back to the agent → attempt 2 … (bounded)
      → report: verified / unverified / stopped, with the checks, changed files and cost

The model decides *how* to reach the goal; HighhX decides *whether* it was reached.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions import events as ev
from highhx.agent.messages import Usage

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.agent.session import AgentSession, TurnResult
    from highhx.commands import App
    from highhx.execution.cancellation import CancellationToken

MAX_ATTEMPTS = 3
FEEDBACK_LINES = 60
TASK_STARTED = "task.started"
TASK_ATTEMPT = "task.attempt"
TASK_VERIFIED = "task.verified"
TASK_COMPLETED = "task.completed"


@dataclass(frozen=True)
class Check:
    """One criterion of the definition of done: an action HighhX runs to verify the task."""

    name: str
    action: str
    command_key: str
    """The project command it needs (``test``, ``lint``, ``build``) — no command, no check."""


CHECKS: dict[str, Check] = {
    "test": Check("tests pass", "project.test", "test"),
    "check": Check("checks pass (lint, types, tests)", "project.check", "lint"),
    "build": Check("the project builds", "project.build", "build"),
}

# Phrases that state a definition of done. Deterministic: the same request, the same checks.
_DONE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(all |the )?tests? (all )?(pass|passes|passing|are green|green|succeed)\b", "test"),
    (r"\b(make|keep|get) (the |all )?tests? (pass|passing|green)\b", "test"),
    (r"\b(run|re-?run) the tests\b.*\b(fix|until|and)\b", "test"),
    (r"\b(lint|linting|linters?|type ?checks?|checks?) (pass|passes|clean|are clean|is clean)\b", "check"),
    (r"\b(it|the project|everything) (builds|compiles)\b|\bbuild (passes|succeeds|works)\b", "build"),
    (r"\bverify (everything|it all|all of it)\b", "test"),
)


def definition_of_done(text: str) -> list[str]:
    """Check keys the request itself asks for (``["test"]`` …), in a stable order."""
    low = " ".join(text.lower().split())
    found = {key for pattern, key in _DONE_PATTERNS if re.search(pattern, low)}
    return [key for key in CHECKS if key in found]


@dataclass
class TaskSpec:
    goal: str
    checks: list[Check] = field(default_factory=list)
    max_attempts: int = MAX_ATTEMPTS
    missing: list[str] = field(default_factory=list)
    """Requested checks the project has no command for (reported, never faked)."""

    @classmethod
    def build(cls, app: App, goal: str, keys: list[str], *, max_attempts: int = MAX_ATTEMPTS) -> TaskSpec:
        commands = app.commands()
        checks: list[Check] = []
        missing: list[str] = []
        for key in dict.fromkeys(keys):
            check = CHECKS.get(key)
            if check is None:
                continue
            if commands.get(check.command_key):
                checks.append(check)
            else:
                missing.append(check.name)
        return cls(goal, checks, max(1, max_attempts), missing)


@dataclass
class CheckResult:
    name: str
    action: str
    ok: bool
    summary: str
    output: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "action": self.action, "ok": self.ok, "summary": self.summary}


@dataclass
class TaskReport:
    goal: str
    status: str
    """verified · unverified (checks still failing) · done (no checks requested) · stopped · cancelled"""
    attempts: int
    checks: list[CheckResult] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("verified", "done")

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "ok": self.ok,
            "attempts": self.attempts,
            "checks": [c.to_dict() for c in self.checks],
            "unavailable_checks": self.missing,
            "changed_files": self.changed_files,
            "summary": self.summary,
            "usage": self.usage.to_dict(),
            "seconds": round(self.seconds, 2),
        }


class _Tee:
    """Forwards engine output to the real sink and keeps the lines (for feedback to the agent)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.lines: list[str] = []

    def stream_line(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None:
        self.lines.append(line)
        self.inner.stream_line(line, stream=stream, source=source)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class TaskRunner:
    """Runs a task to a verified result with the existing agent session and action executor."""

    def __init__(
        self,
        session: AgentSession,
        verifier: ActionExecutor,
        *,
        on_attempt: Callable[[int, int], None] | None = None,
        on_check: Callable[[CheckResult], None] | None = None,
        around_checks: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.session = session
        self.verifier = verifier
        self.on_attempt = on_attempt
        self.on_check = on_check
        self.around_checks = around_checks or contextlib.nullcontext

    @property
    def events(self) -> Any:
        return self.session.app.ctx.events

    def run(self, spec: TaskSpec, *, cancel: CancellationToken | None = None) -> TaskReport:
        started = time.monotonic()
        report = TaskReport(spec.goal, "stopped", 0, missing=list(spec.missing))
        self.events.emit(
            TASK_STARTED, goal=spec.goal, checks=[c.action for c in spec.checks], max_attempts=spec.max_attempts
        )
        changed: list[str] = []
        prompt = self._brief(spec)
        with self.session.app.engine.operation(
            "task", " ".join(spec.goal.split())[:60], metadata={"checks": [c.action for c in spec.checks]}
        ) as op:
            for attempt in range(1, spec.max_attempts + 1):
                report.attempts = attempt
                self.events.emit(TASK_ATTEMPT, goal=spec.goal, attempt=attempt)
                if self.on_attempt is not None:
                    self.on_attempt(attempt, spec.max_attempts)
                result = self.session.run_turn(prompt, cancel=cancel)
                self._account(report, result, changed)
                if result.stopped == "cancelled":
                    report.status = "cancelled"
                    break
                if result.stopped == "refusal":
                    report.status = "stopped"
                    break
                if not spec.checks:
                    report.status = "done" if result.stopped == "completed" else "stopped"
                    break
                report.checks = self.verify(spec)
                failing = [c for c in report.checks if not c.ok]
                if not failing:
                    report.status = "verified"
                    self.events.emit(TASK_VERIFIED, goal=spec.goal, attempt=attempt)
                    break
                report.status = "unverified"
                if attempt < spec.max_attempts:
                    prompt = self._feedback(spec.goal, failing, attempt, spec.max_attempts, result)
            op.metadata.update(report.to_dict())
            if not report.ok:
                from highhx.core.result import Status

                op.status = Status.CANCELLED if report.status == "cancelled" else Status.FAILED
                op.error = f"task {report.status}"
        report.changed_files = list(dict.fromkeys(changed))
        report.seconds = time.monotonic() - started
        self.events.emit(TASK_COMPLETED, goal=spec.goal, status=report.status, attempts=report.attempts)
        return report

    def verify(self, spec: TaskSpec) -> list[CheckResult]:
        """Run every check as a HighhX action; the output is kept for the agent."""
        engine = self.session.app.engine
        results: list[CheckResult] = []
        with self.around_checks():
            return self._verify(spec, engine, results)

    def _verify(self, spec: TaskSpec, engine: Any, results: list[CheckResult]) -> list[CheckResult]:
        for check in spec.checks:
            tee = _Tee(engine.output)
            engine.output = tee
            try:
                outcome = self.verifier.run(check.action, {})
            finally:
                engine.output = tee.inner
            result = CheckResult(check.name, check.action, outcome.ok, outcome.summary or outcome.error, tee.lines)
            results.append(result)
            self.events.emit(ev.ACTION_COMPLETED if outcome.ok else ev.ACTION_FAILED, action=check.action, task=True)
            if self.on_check is not None:
                self.on_check(result)
        return results

    @staticmethod
    def _account(report: TaskReport, result: TurnResult, changed: list[str]) -> None:
        report.usage.add(result.usage)
        report.summary = result.text or report.summary
        changed.extend(result.changed_files)

    @staticmethod
    def _brief(spec: TaskSpec) -> str:
        if not spec.checks:
            return spec.goal
        done = "; ".join(f"{c.name} (HighhX runs the {c.action} action)" for c in spec.checks)
        return (
            f"{spec.goal}\n\n[HighhX task] Definition of done: {done}. When you finish, HighhX runs these "
            "checks itself and sends you any failures. Inspect first, propose a plan for multi-step work, make "
            "focused changes, and run the checks yourself before you report."
        )

    @staticmethod
    def _feedback(goal: str, failing: list[CheckResult], attempt: int, attempts: int, result: TurnResult) -> str:
        parts = [
            f"[HighhX task] Goal: {goal}\nVerification failed after attempt {attempt} of {attempts}. "
            "Find the root cause and fix it; do not weaken or skip checks."
        ]
        if result.stopped in ("max_steps", "max_tokens"):
            parts.append(f"(Your last turn stopped early: {result.stopped}. Continue where you left off.)")
        for check in failing:
            tail = "\n".join(check.output[-FEEDBACK_LINES:]) or check.summary
            parts.append(f"✗ {check.name} ({check.action}): {check.summary}\n{tail}")
        return "\n\n".join(parts)
