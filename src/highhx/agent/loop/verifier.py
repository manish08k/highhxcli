"""AgentVerifier: did the step do what it was for? Never assumed.

    1. the executor's own result (denied, blocked, failed → FAILED, whatever the screen shows)
    2. the step's declarative check, or a default for its verb:
         click / press / select   the screen changed
         type                     the field shows the text (never read from secret fields)
         open                     the browser is on that host
         launch                   that application is in front
    3. UNKNOWN → observe again (after a short settle), up to ``extra_observations`` times,
       before the answer stands. An unknown outcome is never reported as success

Results: SUCCESS · PARTIAL_SUCCESS · FAILED · UNKNOWN.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from highhx.actions import events as ev
from highhx.actions.protocol import Outcome
from highhx.agent.loop.model import StepIntent
from highhx.verification.declarative import Verdict, VerificationContext, VerificationReport, evaluate

if TYPE_CHECKING:
    from highhx.agent.loop.worker import WorkResult
    from highhx.execution.cancellation import CancellationToken
    from highhx.perception.state import ComputerState


@dataclass
class StepVerdict:
    outcome: Outcome
    report: VerificationReport | None
    after: ComputerState | None
    observations: int = 0
    detail: str = ""
    unobservable: bool = False
    """The effect cannot be observed by design (text typed into a secret field): unconfirmed,
    never success — the task's own success check decides."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unobservable": self.unobservable,
            "outcome": str(self.outcome),
            "report": self.report.to_dict() if self.report else None,
            "observations": self.observations,
            "detail": self.detail,
        }


def default_check(step: StepIntent, before: ComputerState | None) -> dict[str, Any] | list[Any] | None:
    if step.verify is not None:
        return step.verify
    verb = step.action
    if verb == "type" and step.label and step.parameters.get("text"):
        element = None
        if before is not None:
            element = next(iter(before.find(name=step.label)), None)
        if element is not None and element.secret:
            return None
        return {"any": [{"element": {"name": step.label, "value": str(step.parameters["text"])}}, {"text": str(step.parameters["text"])}]}
    if verb in ("click", "double_click", "press", "select", "back", "home", "scroll"):
        return {"changed": True} if before is not None else None
    if verb == "open":
        host = urlparse(str(step.parameters.get("url") or step.label)).hostname
        return {"url_contains": host.removeprefix("www.")} if host else None
    if verb == "launch" and before is not None and before.surface == "desktop":
        return {"application": str(step.parameters.get("name") or step.label)}
    return None


def _secret_target(step: StepIntent, before: ComputerState | None, work: WorkResult) -> bool:
    candidate = work.grounding.candidate if work.grounding is not None else None
    if candidate is not None and candidate.element is not None:
        return candidate.element.secret
    if before is not None and step.label:
        return any(e.secret for e in before.find(name=step.label))
    return False


class AgentVerifier:
    def __init__(
        self,
        observe: Callable[[], ComputerState | None],
        *,
        extra_observations: int = 2,
        settle: float = 0.4,
        emit: Callable[..., Any] | None = None,
        cancel: CancellationToken | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.observe = observe
        self.extra = extra_observations
        self.settle = settle
        self.emit = emit
        self.cancel = cancel
        self.sleep = sleep

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            self.sleep(seconds)

    def verify(self, step: StepIntent, work: WorkResult, before: ComputerState | None) -> StepVerdict:
        last = work.last
        if step.action == "wait":
            self._wait(float(step.parameters.get("seconds") or 1.0))
            return StepVerdict(Outcome.SUCCESS, None, self.observe(), 1, "waited")
        if last is None:
            return StepVerdict(Outcome.FAILED, None, before, 0, "nothing ran")
        if not last.result.ok:
            return StepVerdict(Outcome.FAILED, None, before, 0, last.result.error or last.result.status)
        if step.action == "type" and step.verify is None and _secret_target(step, before, work):
            after = self.observe()
            return StepVerdict(Outcome.UNKNOWN, None, after, 1, "typed into a secret field: it cannot be read back", unobservable=True)
        check = default_check(step, before)
        if check is None:
            if last.outcome == Outcome.UNKNOWN and before is not None:
                check = {"changed": True}
            else:
                after = self.observe() if before is not None else None
                return StepVerdict(last.outcome, None, after, 1 if after is not None else 0, last.result.summary)
        if self.emit is not None:
            self.emit(ev.VERIFICATION_STARTED, step=step.id, check=check)
        self._wait(self.settle)
        after = self.observe()
        observations = 1
        ctx = VerificationContext(result=last.result, before=before, after=after, network=last.result.output.get("network"))
        result = evaluate(check, ctx)
        while result.verdict == Verdict.UNKNOWN and observations <= self.extra:
            self._wait(self.settle * (observations + 1))
            ctx.after = after = self.observe()
            observations += 1
            result = evaluate(check, ctx)
        if result.verdict == Verdict.UNSATISFIED and after is not None and observations <= self.extra:
            # a slow page: give it one more look before calling it a failure
            self._wait(self.settle * 2)
            ctx.after = later = self.observe()
            observations += 1
            again = evaluate(check, ctx)
            if again.verdict == Verdict.SATISFIED:
                result, after = again, later
        report = VerificationReport(result.verdict, result, observations)
        if report.verdict == Verdict.SATISFIED:
            outcome = Outcome.SUCCESS
        elif report.partial:
            outcome = Outcome.PARTIAL_SUCCESS
        elif report.verdict == Verdict.UNSATISFIED:
            outcome = Outcome.FAILED
        else:
            outcome = Outcome.UNKNOWN
        if self.emit is not None:
            self.emit(ev.VERIFICATION_COMPLETED, step=step.id, outcome=str(outcome), verdict=str(report.verdict), observations=observations)
            if outcome == Outcome.FAILED:
                self.emit(ev.VERIFICATION_FAILED, step=step.id, detail=result.detail)
        return StepVerdict(outcome, report, after, observations, result.detail)
