"""The unified action protocol: one request shape for every surface, one executor for all of them.

    ActionRequest(action_type="desktop.click", intent="save the document",
                  target={"label": "Save", "role": "button"},
                  parameters={"x": 840, "y": 512},
                  grounding=({"strategy": "accessibility", "result": "success"},),
                  verification={"absent": {"role": "button", "name": "Save"}})
        │ resolve: namespaced aliases → catalog actions (desktop.click → computer.click_at)
        ▼
    ActionExecutor.plan        validate inputs · classify · catalog floor · policy name
        │ semantic floor: the target's label is classified too, so a point that a vision model
        │                 grounded on "Delete account" is rated like the button itself. The label
        │                 can only raise the risk, never lower it
        ▼
    ActionExecutor.execute     policy · approval · run · the action's own verification · audit
        ▼
    declarative verification   (verification/declarative.py) → SUCCESS · PARTIAL_SUCCESS ·
                                                                 FAILED · UNKNOWN

A request never carries its own risk or approval. Those are always computed by HighhX. A caller
may only ask for a *higher* floor (``min_risk``). Retries beyond the executor's own are made only
for actions that cannot do harm twice: idempotent ones, or SAFE ones. A risky action is never
repeated silently. Every event a request causes carries its ``trace_id`` and ``action_id``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from highhx.actions import events as ev
from highhx.actions.policy import APPROVAL_RULES, Approval, Decision, Risk, decide
from highhx.core.events import new_id, trace_context
from highhx.execution.retry import RetryPolicy
from highhx.safety.actions import ActionKind, attrs

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor, ActionNode, Planned
    from highhx.actions.spec import ActionResult
    from highhx.execution.cancellation import CancellationToken
    from highhx.perception.state import ComputerState
    from highhx.verification.declarative import VerificationReport


class Outcome(StrEnum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    """It ran, but nothing confirmed its effect. Observe again; never report it as done."""


Transform = Callable[[dict[str, Any]], dict[str, Any]]

ACTION_ALIASES: dict[str, tuple[str, Transform | None]] = {
    "desktop.click": ("computer.click_at", None),
    "desktop.double_click": ("computer.click_at", lambda p: {**p, "count": 2}),
    "desktop.right_click": ("computer.click_at", lambda p: {**p, "button": "right"}),
    "desktop.type": ("computer.type", None),
    "desktop.key": ("computer.press", None),
    "desktop.hotkey": ("computer.hotkey", None),
    "desktop.scroll": ("computer.scroll", lambda p: {"source": "desktop", **p}),
    "desktop.launch": ("computer.launch", None),
    "desktop.focus": ("computer.focus", None),
    "desktop.move": ("computer.move", None),
    "desktop.drag": ("computer.drag", None),
    "desktop.close": ("computer.quit", None),
    "desktop.observe": ("computer.observe", None),
    "desktop.screenshot": ("computer.screenshot", None),
    "desktop.menu": ("computer.menu", None),
    "browser.type": ("browser.fill", None),
    "browser.navigate": ("browser.open", None),
    "shell.exec": ("shell.run", None),
}
"""Namespaced action types (``surface.verb``) that are another name for a catalog action."""

UI_KINDS = frozenset(
    {
        ActionKind.UI_CLICK,
        ActionKind.UI_TYPE,
        ActionKind.UI_KEY,
        ActionKind.UI_SELECT,
        ActionKind.UI_SCROLL,
        ActionKind.UI_UPLOAD,
        ActionKind.NAVIGATE,
        ActionKind.APP_LAUNCH,
    }
)
"""Kinds whose effect is on a screen: a handler's "ok" is not proof of the effect."""

SECRET_PARAMS = ("text", "content", "password", "value")


def resolve(action_type: str, parameters: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The catalog action and inputs for ``action_type`` (aliases applied)."""
    params = dict(parameters or {})
    alias = ACTION_ALIASES.get(action_type)
    if alias is None:
        return action_type, params
    name, transform = alias
    return name, transform(params) if transform else params


@dataclass(frozen=True)
class ActionRequest:
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    """Why, in words (shown in traces, used by trajectory memory). Never used to lower risk."""
    target: dict[str, Any] = field(default_factory=dict)
    """The semantic target: ``{label, role, selectors…}`` (see :mod:`highhx.grounding`)."""
    grounding: tuple[dict[str, Any], ...] = ()
    """How the target was found: one entry per strategy tried, in order."""
    environment: str = "local"
    """The runtime the action runs in (local · sandbox:<id> · ssh://…)."""
    min_risk: Risk | None = None
    """A higher risk floor than the catalog's (it can never lower one)."""
    timeout: float | None = None
    """At most this many seconds (it can shorten the action's own timeout, not lengthen it)."""
    retry_policy: RetryPolicy | None = None
    verification: dict[str, Any] | list[Any] | None = None
    verify_timeout: float = 0.0
    rollback: bool = False
    """Compensate this action if a later step of its graph fails."""
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_action: str | None = None
    trace_id: str | None = None
    id: str = field(default_factory=lambda: new_id("act"))

    @property
    def label(self) -> str:
        return str(self.target.get("label") or self.target.get("name") or self.target.get("text") or "")

    def redacted(self) -> dict[str, Any]:
        """``to_dict`` with typed text replaced by its length (for logs, traces, trajectories)."""
        data = self.to_dict()
        data["parameters"] = {
            k: (f"<{len(str(v))} characters>" if k in SECRET_PARAMS and isinstance(v, str) else v)
            for k, v in self.parameters.items()
        }
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action_type": self.action_type,
            "intent": self.intent,
            "target": dict(self.target),
            "parameters": dict(self.parameters),
            "grounding": [dict(g) for g in self.grounding],
            "environment": self.environment,
            "min_risk": self.min_risk.label if self.min_risk is not None else None,
            "timeout": self.timeout,
            "retry_policy": (
                {"attempts": self.retry_policy.attempts, "delay": self.retry_policy.delay}
                if self.retry_policy
                else None
            ),
            "verification": self.verification,
            "verify_timeout": self.verify_timeout,
            "rollback": self.rollback,
            "metadata": dict(self.metadata),
            "parent_action": self.parent_action,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionRequest:
        retry = data.get("retry_policy")
        return cls(
            action_type=str(data["action_type"]),
            parameters=dict(data.get("parameters") or {}),
            intent=str(data.get("intent") or ""),
            target=dict(data.get("target") or {}),
            grounding=tuple(dict(g) for g in data.get("grounding") or ()),
            environment=str(data.get("environment") or "local"),
            min_risk=Risk.parse(data["min_risk"]) if data.get("min_risk") else None,
            timeout=float(data["timeout"]) if data.get("timeout") else None,
            retry_policy=RetryPolicy.from_value(retry) if retry else None,
            verification=data.get("verification"),
            verify_timeout=float(data.get("verify_timeout") or 0.0),
            rollback=bool(data.get("rollback", False)),
            metadata=dict(data.get("metadata") or {}),
            parent_action=data.get("parent_action"),
            trace_id=data.get("trace_id"),
            id=str(data.get("id") or new_id("act")),
        )

    def child(self, action_type: str, parameters: dict[str, Any], **changes: Any) -> ActionRequest:
        """A follow-up request (a recovery attempt, a sub-step) linked to this one."""
        return replace(
            self,
            action_type=action_type,
            parameters=parameters,
            parent_action=self.id,
            id=new_id("act"),
            **changes,
        )


@dataclass
class PreparedAction:
    request: ActionRequest
    action: str
    """The catalog action the request resolved to."""
    planned: Planned

    @property
    def risk(self) -> Risk:
        return self.planned.decision.risk

    @property
    def approval(self) -> Approval:
        return self.planned.decision.approval

    @property
    def blocked(self) -> bool:
        return self.planned.decision.blocked

    def to_dict(self) -> dict[str, Any]:
        return {"request": self.request.redacted(), "action": self.action, **self.planned.to_dict()}


@dataclass
class ActionResponse:
    request: ActionRequest
    action: str
    outcome: Outcome
    result: ActionResult
    risk: Risk
    approval: Approval
    verification: VerificationReport | None = None
    attempts: int = 1
    seconds: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.SUCCESS

    @property
    def status(self) -> str:
        """The executor's status (ok · failed · denied · blocked · cancelled · timeout · planned)."""
        return self.result.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.request.id,
            "action_type": self.request.action_type,
            "action": self.action,
            "outcome": str(self.outcome),
            "status": self.result.status,
            "risk": self.risk.label,
            "approval": self.approval.name.lower(),
            "summary": self.result.summary,
            "error": self.result.error,
            "verified": self.result.verified,
            "verification": self.verification.to_dict() if self.verification else None,
            "attempts": self.attempts,
            "seconds": round(self.seconds, 3),
            "reasons": list(self.reasons),
            "trace_id": self.request.trace_id,
            "parent_action": self.request.parent_action,
        }


def prepare(executor: ActionExecutor, request: ActionRequest) -> PreparedAction:
    """Plan ``request``: resolve, validate, classify — including its semantic target. Raises for
    unknown actions and invalid inputs (nothing has run)."""
    name, params = resolve(request.action_type, request.parameters)
    planned = executor.plan(name, params)
    spec = planned.spec
    if request.timeout is not None and request.timeout < spec.timeout:
        planned.spec = replace(spec, timeout=max(1.0, request.timeout))
    label = request.label
    role = str(request.target.get("role") or "")
    descriptor = planned.descriptor
    if label and spec.kind in UI_KINDS and descriptor.attr("name") != label:
        extra = dict(descriptor.attributes)
        extra.setdefault("name", label)
        if role:
            extra.setdefault("role", role)
        extra["intent_label"] = label
        descriptor = replace(descriptor, attributes=attrs(**extra))
        planned.descriptor = descriptor
    verdict = executor.gate.classify(descriptor)
    floor = max(planned.decision.risk, request.min_risk or Risk.SAFE)
    semantic = decide(floor, verdict, command=descriptor.command)
    previous = planned.decision
    if semantic.risk > previous.risk or semantic.blocked or floor > previous.risk:
        risk = max(semantic.risk, previous.risk, floor)
        planned.decision = Decision(
            risk,
            APPROVAL_RULES[risk],
            tuple(dict.fromkeys((*previous.reasons, *semantic.reasons))),
            previous.blocked or semantic.blocked,
        )
    return PreparedAction(request, name, planned)


def _repeatable(prepared: PreparedAction, result: ActionResult) -> bool:
    """Whether trying again cannot do harm twice."""
    if result.ok or not result.retryable or result.status != "failed":
        return False
    return prepared.planned.spec.idempotent or prepared.risk == Risk.SAFE


def classify(
    prepared: PreparedAction, result: ActionResult, report: VerificationReport | None
) -> Outcome:
    from highhx.verification.declarative import Verdict

    if not result.ok:
        return Outcome.FAILED
    if report is not None:
        if report.verdict == Verdict.SATISFIED:
            return Outcome.SUCCESS
        if report.partial:
            return Outcome.PARTIAL_SUCCESS
        return Outcome.FAILED if report.verdict == Verdict.UNSATISFIED else Outcome.UNKNOWN
    if result.verified is True:
        return Outcome.SUCCESS
    if result.verified is False:
        return Outcome.FAILED
    return Outcome.UNKNOWN if prepared.planned.spec.kind in UI_KINDS else Outcome.SUCCESS


def submit(
    executor: ActionExecutor,
    request: ActionRequest,
    *,
    before: ComputerState | None = None,
    observe: Callable[[], ComputerState] | None = None,
    network: list[dict[str, Any]] | None = None,
    cancel: CancellationToken | None = None,
    preapproved: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ActionResponse:
    """Run ``request`` through the executor, verify it, and classify the outcome. Never raises
    for denied, blocked or failed actions (see ``outcome`` and ``status``); raises only for
    unknown actions and invalid inputs, before anything runs."""
    started = time.monotonic()
    with trace_context(trace_id=request.trace_id, action_id=request.id):
        prepared = prepare(executor, request)
        return _run(executor, prepared, before, observe, network, cancel, preapproved, sleep, started)


def run_prepared(
    executor: ActionExecutor,
    prepared: PreparedAction,
    *,
    before: ComputerState | None = None,
    observe: Callable[[], ComputerState] | None = None,
    cancel: CancellationToken | None = None,
    preapproved: bool = False,
) -> ActionResponse:
    """Run an action already shown to the person with :func:`prepare` (a preview)."""
    with trace_context(trace_id=prepared.request.trace_id, action_id=prepared.request.id):
        return _run(executor, prepared, before, observe, None, cancel, preapproved, time.sleep, time.monotonic())


def _run(
    executor: ActionExecutor,
    prepared: PreparedAction,
    before: ComputerState | None,
    observe: Callable[[], ComputerState] | None,
    network: list[dict[str, Any]] | None,
    cancel: CancellationToken | None,
    preapproved: bool,
    sleep: Callable[[float], None],
    started: float,
) -> ActionResponse:
    request = prepared.request
    policy = request.retry_policy or RetryPolicy()
    attempts = 0
    while True:
        attempts += 1
        result = executor.execute(prepared.planned, cancel=cancel, preapproved=preapproved)
        if attempts >= policy.attempts or not _repeatable(prepared, result):
            break
        delay = policy.delay_before(attempts)
        executor.events.emit(
            ev.ACTION_RETRY, action=prepared.action, attempt=attempts, delay=delay, error=result.error, source="protocol"
        )
        if delay:
            sleep(delay)
    report = None
    if request.verification is not None and result.ok:
        from highhx.verification.declarative import VerificationContext, verify

        executor.events.emit(ev.VERIFICATION_STARTED, action=prepared.action, spec=request.verification)
        ctx = VerificationContext(
            result=result,
            before=before,
            after=observe() if observe is not None else None,
            root=executor.app.root,
            observe=observe,
            network=network,
        )
        report = verify(request.verification, ctx, timeout=request.verify_timeout, cancel=cancel)
        executor.events.emit(
            ev.VERIFICATION_COMPLETED,
            action=prepared.action,
            verdict=str(report.verdict),
            partial=report.partial,
            samples=report.samples,
        )
    outcome = classify(prepared, result, report)
    return ActionResponse(
        request,
        prepared.action,
        outcome,
        result,
        prepared.risk,
        prepared.approval,
        report,
        attempts,
        time.monotonic() - started,
        prepared.planned.decision.reasons,
    )


def to_node(request: ActionRequest, depends_on: tuple[str, ...] = (), continue_on_error: bool = False) -> ActionNode:
    """The request as a node of an action graph (:func:`highhx.actions.executor.run_graph`)."""
    from highhx.actions.executor import ActionNode

    name, params = resolve(request.action_type, request.parameters)
    return ActionNode(request.id, name, params, depends_on, continue_on_error)
