"""The action executor: the one way anything runs.

    resolve / propose → validate inputs → classify (deterministic) → decide risk & approval
      → project policy → approval (bound to the exact action) → execute (timeout, cancellation,
        retries only for idempotent actions) → verify → audit + history + events

Free (typed commands, resolved plain language, workflow steps) and Pro (the AI agent's
action graphs) use the same executor; only the ``actor`` differs, and with it what may be
pre-approved (see :mod:`highhx.actions.policy`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions import events as ev
from highhx.actions.catalog import Catalog, default_catalog
from highhx.actions.policy import Approval, Decision, decide
from highhx.actions.spec import ActionContext, ActionResult, ActionSpec, Inputs
from highhx.agent.tools.base import ToolError
from highhx.agent.tools.files import ChangeJournal
from highhx.core.errors import (
    ApprovalDeniedError,
    HighhXError,
    OperationCancelledError,
    PolicyViolationError,
    ValidationError,
)
from highhx.core.result import Status
from highhx.execution.cancellation import CancellationToken
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor
from highhx.safety.gate import ActionGate, ApprovalMode, GatePrompter

if TYPE_CHECKING:
    from highhx.commands import App
    from highhx.computer.driver import HighhXDriver
    from highhx.computer.session import ComputerSession


class UnknownActionError(ValidationError):
    pass


_WHAT = {
    ActionKind.WRITE_FILE: "changes files in the project",
    ActionKind.DELETE_FILE: "deletes files",
    ActionKind.GIT: "changes the git repository",
    ActionKind.EXEC: "runs commands",
    ActionKind.DEPLOY: "deploys",
    ActionKind.ROLLBACK: "rolls a deployment back",
    ActionKind.NAVIGATE: "opens a web page",
    ActionKind.APP_LAUNCH: "launches an application",
}


def why(spec: ActionSpec, risk: Any) -> str:
    """One plain reason for an approval prompt, when the classifier gave none."""
    what = _WHAT.get(spec.kind, "acts on the project")
    return f"{spec.name} {what} (rated {risk.label})"


@dataclass
class Planned:
    """A validated action with its risk decision — what a preview shows before anything runs."""

    spec: ActionSpec
    inputs: Inputs
    descriptor: ActionDescriptor
    decision: Decision
    actor: Actor

    @property
    def asks(self) -> bool:
        return self.decision.asks_for(self.actor)

    changes: list[str] = field(default_factory=list)
    """What exactly will change (diffs, deleted lines …), computed when planned."""

    def preview(self) -> list[str]:
        lines = [f"{self.spec.name} — {self.spec.description}"]
        command = self.spec.command_for(self.inputs)
        if command:
            lines.append(f"command: {command}")
        target = self.spec.target_for(self.inputs)
        if target:
            lines.append(f"target: {target}")
        shown = {k: v for k, v in self.inputs.items() if k not in ("content",)}
        if shown and not command:
            lines.append("inputs: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
        lines.append(f"risk: {self.decision.risk.label}" + (" (blocked)" if self.decision.blocked else ""))
        lines += [f"why: {r}" for r in self.decision.reasons[:3]]
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.spec.name,
            "inputs": {k: v for k, v in self.inputs.items() if k != "content"},
            "risk": self.decision.risk.label,
            "approval": self.decision.approval.name.lower(),
            "asks": self.asks,
            "blocked": self.decision.blocked,
            "reasons": list(self.decision.reasons),
        }


class ActionExecutor:
    def __init__(
        self,
        app: App,
        gate: ActionGate,
        *,
        actor: Actor = Actor.USER,
        catalog: Catalog | None = None,
        journal: ChangeJournal | None = None,
        computer: Callable[[], ComputerSession] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.app = app
        self.gate = gate
        self.actor = actor
        self.catalog = catalog or default_catalog()
        self.journal = journal or ChangeJournal()
        self._computer_factory = computer
        self._computer: ComputerSession | None = None
        self._sleep = sleep

    @classmethod
    def for_user(cls, app: App, prompter: GatePrompter, **kwargs: Any) -> ActionExecutor:
        """An executor for the user's own deterministic actions (Free and Pro alike)."""
        from highhx.safety.audit import AuditLog

        gate = ActionGate(
            app.engine,
            prompter,
            source="actions",
            mode=ApprovalMode.ASK,
            assume_yes=app.options.yes,
            audit=AuditLog(app.db, app.redactor) if app.db is not None else None,
        )
        if "catalog" not in kwargs:
            from highhx.actions.catalog import catalog_for

            kwargs["catalog"] = catalog_for(app)
        return cls(app, gate, actor=Actor.USER, **kwargs)

    # -------------------------------------------------------------- helpers
    @property
    def events(self) -> Any:
        return self.app.ctx.events

    def computer(self) -> ComputerSession:
        if self._computer_factory is not None:
            return self._computer_factory()
        if self._computer is None:
            from highhx.computer.session import ComputerSession

            self._computer = ComputerSession(self.gate, actor=self.actor, tool="actions")
        return self._computer

    def driver(self, cancel: CancellationToken | None = None) -> HighhXDriver:
        """The HighhX Computer API for desktop actions — the computer session's, so HighhX Free's
        actions and HighhX Pro's agent tools share one runtime and one engine."""
        session = self.computer()
        if cancel is not None:
            session.cancel = cancel
        return session.driver()

    def close(self) -> None:
        if self._computer is not None:
            self._computer.close()
            self._computer = None

    # ----------------------------------------------------------------- plan
    def plan(self, name: str, inputs: Inputs | None = None) -> Planned:
        """Validate and rate an action without running it. Raises for unknown actions / bad inputs."""
        spec = self.catalog.get(name)
        if spec is None:
            raise UnknownActionError(
                f"Unknown action '{name}'.", hint="See `highhx actions` for the catalog (`/tools` in the session)."
            )
        data = dict(inputs or {})
        problems = spec.validate(data)
        if problems:
            raise ValidationError(f"Invalid input for {spec.name}.", details=problems)
        descriptor = ActionDescriptor(
            kind=spec.kind,
            summary=spec.describe(data),
            tool=spec.name,
            target=spec.target_for(data),
            application=f"project {self.app.root.name}",
            command=spec.command_for(data),
            environment=spec.environment_for(data),
            actor=self.actor,
        )
        verdict = self.gate.classify(descriptor)
        floor = max(spec.risk, spec.risk_for(data)) if spec.risk_for is not None else spec.risk
        decision = decide(floor, verdict, command=descriptor.command)
        changes: list[str] = []
        if spec.preview is not None:
            try:
                changes = spec.preview(self.app, data)
            except (HighhXError, ToolError, OSError):
                changes = []  # the handler reports the real problem when it runs
        return Planned(spec, data, descriptor, decision, self.actor, changes)

    # ------------------------------------------------------------------ run
    def run(self, name: str, inputs: Inputs | None = None, *, cancel: CancellationToken | None = None) -> ActionResult:
        """Plan and execute one action. Never raises for denied/blocked/failed actions (see the
        result's ``status``); raises only for unknown actions and invalid inputs."""
        return self.execute(self.plan(name, inputs), cancel=cancel)

    def execute(
        self, planned: Planned, *, cancel: CancellationToken | None = None, preapproved: bool = False
    ) -> ActionResult:
        """Execute a planned action. ``preapproved``: the person already approved exactly this
        previewed action (``/approve``) — honoured only for their own actions below CRITICAL,
        the same rule as ``--yes``; the agent's actions and critical ones always ask."""
        spec, inputs, decision = planned.spec, planned.inputs, planned.decision
        started = time.monotonic()
        self.events.emit(ev.ACTION_PLANNED, actor=str(self.actor), **planned.to_dict())
        if self.app.options.dry_run:
            return ActionResult(True, summary="dry run: " + "; ".join(planned.preview()), status="planned")
        preapproved = preapproved and self.actor == Actor.USER and decision.approval < Approval.TYPED
        asks = planned.asks and not preapproved
        if asks:
            self.events.emit(ev.APPROVAL_REQUESTED, action=spec.name, risk=decision.risk.label)
        assume_yes = self.gate.assume_yes
        if preapproved:
            self.gate.assume_yes = True
            self.events.emit(ev.APPROVAL_GRANTED, action=spec.name, risk=decision.risk.label, mode="preapproved")
        try:
            authorization = self.gate.authorize(
                planned.descriptor,
                policy_action=spec.policy_name(inputs),
                grant=spec.name if decision.approval <= Approval.MODE else None,
                details=planned.changes,
                always_confirm=decision.approval >= Approval.ASK,
                min_risk=decision.risk.level,
                min_risk_reason=why(spec, decision.risk),
                risk_label=decision.risk.label,
            )
        except PolicyViolationError as exc:
            self.events.emit(ev.ACTION_FAILED, action=spec.name, status="blocked", error=exc.message)
            return ActionResult(False, status="blocked", error=exc.message, summary="blocked by policy")
        except ApprovalDeniedError as exc:
            self.events.emit(ev.APPROVAL_DENIED, action=spec.name, risk=decision.risk.label)
            return ActionResult(False, status="denied", error=exc.message, summary="not approved")
        finally:
            self.gate.assume_yes = assume_yes
        if asks:
            self.events.emit(ev.APPROVAL_GRANTED, action=spec.name, risk=decision.risk.label)

        token = (cancel or self.app.ctx.cancel).child()
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            token.cancel("timeout")

        timer = threading.Timer(spec.timeout, expire)
        timer.daemon = True
        previous = self.app.ctx.cancel
        self.app.ctx.cancel = token
        ctx = ActionContext(self.app, self, token, self.actor, self.journal, self.computer)
        result = ActionResult(False, status="failed")
        timer.start()
        try:
            with (
                self.app.engine.operation("action", spec.name, metadata={"actor": str(self.actor)}) as op,
                self.gate.executing(authorization) as audit,
            ):
                result = self._attempts(spec, ctx, inputs, token, timed_out)
                if result.ok and spec.verify is not None:
                    verified, detail = spec.verify(ctx, inputs, result)
                    result.verified = verified
                    if not verified:
                        result.ok, result.status, result.error = False, "failed", f"verification failed: {detail}"
                audit.verified = result.verified if result.verified is not None else result.ok
                if not result.ok:
                    audit.status, audit.error = (
                        ("cancelled" if result.status == "cancelled" else "failed"),
                        result.error,
                    )
                    op.status = Status.CANCELLED if result.status == "cancelled" else Status.FAILED
                    op.error = result.error or result.summary
                    op.exit_code = 130 if result.status == "cancelled" else 1
        except (KeyboardInterrupt, OperationCancelledError):
            result = ActionResult(False, status="timeout" if timed_out.is_set() else "cancelled", error="cancelled")
        finally:
            timer.cancel()
            self.app.ctx.cancel = previous
        result.seconds = time.monotonic() - started
        if result.ok:
            self.events.emit(ev.ACTION_COMPLETED, action=spec.name, summary=result.summary, seconds=result.seconds)
        else:
            self.events.emit(ev.ACTION_FAILED, action=spec.name, status=result.status, error=result.error)
        return result

    def _attempts(
        self,
        spec: ActionSpec,
        ctx: ActionContext,
        inputs: Inputs,
        token: CancellationToken,
        timed_out: threading.Event,
    ) -> ActionResult:
        policy = spec.retries
        result = ActionResult(False)
        for attempt in range(1, policy.attempts + 1):
            if token.cancelled:
                raise OperationCancelledError("cancelled")
            self.events.emit(ev.ACTION_STARTED, action=spec.name, attempt=attempt)
            try:
                result = spec.handler(ctx, inputs)
            except (KeyboardInterrupt, OperationCancelledError):
                if timed_out.is_set():
                    return ActionResult(False, status="timeout", error=f"timed out after {spec.timeout:g}s")
                raise
            except ToolError as exc:
                result = ActionResult(False, error=str(exc), summary=str(exc), retryable=False)
            except HighhXError as exc:
                result = ActionResult(
                    False, error=exc.message + (f" ({exc.hint})" if exc.hint else ""), summary=exc.message
                )
            result.attempts = attempt
            if timed_out.is_set():
                return ActionResult(
                    False, status="timeout", error=f"timed out after {spec.timeout:g}s", attempts=attempt
                )
            if result.ok or attempt == policy.attempts or not result.retryable:
                return result
            delay = policy.delay_before(attempt)
            self.events.emit(ev.ACTION_RETRY, action=spec.name, attempt=attempt, delay=delay, error=result.error)
            if self._sleep is not None:
                self._sleep(delay)
            elif token.wait(delay):
                raise OperationCancelledError("cancelled")
        return result

    # ----------------------------------------------------------- compensation
    def compensate(self, planned: Planned, result: ActionResult) -> str | None:
        """Undo a completed action (rollback). Returns what was done, or None when it cannot be undone.
        Compensations that are themselves risky actions go through approval again."""
        spec = planned.spec
        if spec.compensate is None or not result.ok:
            return None
        ctx = ActionContext(self.app, self, self.app.ctx.cancel, self.actor, self.journal, self.computer)
        try:
            done = spec.compensate(ctx, planned.inputs, result)
        except HighhXError as exc:
            done = f"compensation failed: {exc.message}"
        except ToolError as exc:
            done = f"compensation failed: {exc}"
        result.compensated = not done.startswith("compensation failed")
        self.events.emit(ev.ACTION_COMPENSATED, action=spec.name, summary=done)
        return done


# ------------------------------------------------------------------ graphs
@dataclass
class ActionNode:
    id: str
    action: str
    inputs: Inputs = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    continue_on_error: bool = False


@dataclass
class GraphResult:
    results: dict[str, ActionResult]
    order: list[str]
    ok: bool
    stopped_at: str | None = None
    compensations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stopped_at": self.stopped_at,
            "steps": [{"id": n, **self.results[n].to_dict()} for n in self.order if n in self.results],
            "compensations": self.compensations,
        }


class GraphError(ValidationError):
    pass


def validate_graph(executor: ActionExecutor, nodes: Sequence[ActionNode]) -> list[Planned]:
    """Validate a whole graph before anything runs: unique ids, known dependencies, no cycles,
    every action known and every input valid. Returns the plans in execution order."""
    ids = [n.id for n in nodes]
    if len(set(ids)) != len(ids):
        raise GraphError("Action graph ids must be unique.")
    known = set(ids)
    for node in nodes:
        missing = [d for d in node.depends_on if d not in known]
        if missing:
            raise GraphError(f"Step '{node.id}' depends on unknown step(s): {', '.join(missing)}.")
    order: list[ActionNode] = []
    done: set[str] = set()
    pending = list(nodes)
    while pending:
        ready = [n for n in pending if all(d in done for d in n.depends_on)]
        if not ready:
            raise GraphError("Action graph has a dependency cycle.")
        for node in ready:  # stable: declaration order among ready nodes
            order.append(node)
            done.add(node.id)
            pending.remove(node)
    return [executor.plan(n.action, n.inputs) for n in order]


def run_graph(
    executor: ActionExecutor,
    nodes: Sequence[ActionNode],
    *,
    cancel: CancellationToken | None = None,
    rollback: bool = False,
    on_step: Callable[[ActionNode, Planned], None] | None = None,
    on_result: Callable[[ActionNode, ActionResult], None] | None = None,
) -> GraphResult:
    """Run a validated graph step by step (deterministic order). Stops at the first failure
    (unless the step allows it); with ``rollback``, completed steps are compensated in reverse."""
    plans = validate_graph(executor, nodes)
    by_id = {n.id: n for n in nodes}
    order_ids = _topological_ids(nodes)
    results: dict[str, ActionResult] = {}
    completed: list[tuple[Planned, ActionResult]] = []
    stopped: str | None = None
    for node_id, planned in zip(order_ids, plans, strict=True):
        node = by_id[node_id]
        if any(not results[d].ok and not by_id[d].continue_on_error for d in node.depends_on if d in results):
            results[node_id] = ActionResult(False, status="skipped", summary="skipped: a dependency failed")
            continue
        if on_step is not None:
            on_step(node, planned)
        result = executor.execute(planned, cancel=cancel)
        results[node_id] = result
        if on_result is not None:
            on_result(node, result)
        if result.ok:
            completed.append((planned, result))
        elif not node.continue_on_error:
            stopped = node_id
            break
    compensations: list[str] = []
    if stopped is not None and rollback:
        for planned, result in reversed(completed):
            done = executor.compensate(planned, result)
            if done:
                compensations.append(f"{planned.spec.name}: {done}")
    ok = stopped is None and all(
        r.ok or by_id[i].continue_on_error for i, r in results.items() if r.status != "skipped"
    )
    return GraphResult(results, order_ids, ok, stopped, compensations)


def _topological_ids(nodes: Sequence[ActionNode]) -> list[str]:
    done: list[str] = []
    pending = list(nodes)
    while pending:
        ready = [n for n in pending if all(d in done for d in n.depends_on)]
        for node in ready:
            done.append(node.id)
            pending.remove(node)
    return done
