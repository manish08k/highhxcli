"""The HighhX engine: the single gateway for side-effecting actions.

Every command HighhX runs on the user's behalf goes through :meth:`Engine.run`:

    policy check → risk classification → approval → dry-run → execute → record

Domain services (deployment, release, git …) depend on the engine rather than
on subprocess directly, which keeps safety guarantees in one place.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import os
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from highhx.approvals.manager import ApprovalDecision, ApprovalManager, ApprovalRequest
from highhx.approvals.risk import RiskLevel, classify_command
from highhx.core.context import ExecutionContext
from highhx.core.errors import (
    CommandFailedError,
    HighhXError,
    OperationCancelledError,
    PolicyViolationError,
    TimeoutExpiredError,
)
from highhx.core.events import APPROVAL_DECIDED, APPROVAL_REQUESTED
from highhx.core.executor import Executor
from highhx.core.result import CommandResult, Status
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec
from highhx.observability.tracing import Tracer
from highhx.policy.engine import PolicyDecision, PolicyEngine
from highhx.policy.rules import Effect, PolicyContext
from highhx.security.secrets import Redactor
from highhx.storage.history import HistoryStore
from highhx.storage.logs import LogStore, LogWriter


class OutputSink(Protocol):
    """Subset of :class:`highhx.ui.output.Output` the engine needs."""

    def stream_line(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None: ...

    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def detail(self, message: str) -> None: ...


class NullSink:
    """Output sink that discards everything."""

    def stream_line(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warn(self, message: str) -> None:
        return None

    def detail(self, message: str) -> None:
        return None


@dataclass
class Operation:
    """A recorded unit of user-visible work (one history entry)."""

    kind: str
    name: str
    execution_id: str | None = None
    log: LogWriter | None = None
    status: Status = Status.SUCCESS
    exit_code: int | None = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    nested: bool = False
    started: float = field(default_factory=time.monotonic)

    def fail(self, message: str, *, exit_code: int = 1) -> None:
        self.status = Status.FAILED
        self.error = message
        self.exit_code = exit_code


_current_operation: contextvars.ContextVar[Operation | None] = contextvars.ContextVar("highhx_operation", default=None)


class Engine:
    """Coordinates policy, approvals, dry-run, execution, logging and history."""

    def __init__(
        self,
        ctx: ExecutionContext,
        *,
        approvals: ApprovalManager,
        executor: Executor | None = None,
        policy: PolicyEngine | None = None,
        history: HistoryStore | None = None,
        logs: LogStore | None = None,
        redactor: Redactor | None = None,
        output: OutputSink | None = None,
        tracer: Tracer | None = None,
        project_name: str | None = None,
        facts: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.ctx = ctx
        self.approvals = approvals
        self.executor = executor or Executor(ctx.events)
        self.policy = policy or PolicyEngine()
        self.history = history
        self.logs = logs
        self.redactor = redactor or Redactor()
        self.output: OutputSink = output or NullSink()
        self.tracer = tracer
        self.project_name = project_name
        self._facts_provider = facts
        self._facts: dict[str, Any] | None = None
        self.redactor.add_environment(os.environ)
        self.redactor.add_environment(ctx.env)

    # ----------------------------------------------------------------- facts
    def facts(self) -> dict[str, Any]:
        """Lazily computed facts (current branch, env profile) for policy evaluation."""
        if self._facts is None:
            try:
                self._facts = dict(self._facts_provider()) if self._facts_provider else {}
            except Exception:
                self._facts = {}
        return self._facts

    @property
    def dry_run(self) -> bool:
        return self.ctx.options.dry_run

    @property
    def current_operation(self) -> Operation | None:
        return _current_operation.get()

    # ------------------------------------------------------------ operations
    @contextlib.contextmanager
    def operation(
        self,
        kind: str,
        name: str,
        *,
        command: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Operation]:
        """Record a unit of work in history. Nested operations become steps of the outer one."""
        parent = _current_operation.get()
        if parent is not None:
            op = Operation(kind, name, parent.execution_id, parent.log, nested=True)
            token = _current_operation.set(op)
            try:
                yield op
            except BaseException as exc:
                self._apply_exception(op, exc)
                raise
            finally:
                _current_operation.reset(token)
                if self.history is not None and parent.execution_id:
                    self.history.add_step(
                        parent.execution_id,
                        f"{kind}:{name}",
                        str(op.status),
                        exit_code=op.exit_code,
                        duration=time.monotonic() - op.started,
                        error=op.error,
                    )
            return

        op = Operation(kind, name, metadata=dict(metadata or {}))
        if self.ctx.options.dry_run:
            op.metadata["dry_run"] = True
        if self.history is not None:
            op.execution_id = self.history.start(
                kind,
                name,
                command=command,
                project=self.project_name,
                cwd=str(self.ctx.cwd),
                metadata=op.metadata,
                trace_id=self.tracer.trace_id if self.tracer else None,
            )
            if self.logs is not None:
                op.log = self.logs.writer(op.execution_id)
                op.log.event(f"{kind} {name} started" + (f": {command}" if command else ""))
        token = _current_operation.set(op)
        span_cm = self.tracer.span(f"{kind}:{name}") if self.tracer else contextlib.nullcontext()
        try:
            with span_cm:
                yield op
        except BaseException as exc:
            self._apply_exception(op, exc)
            raise
        finally:
            _current_operation.reset(token)
            duration = time.monotonic() - op.started
            if op.log is not None:
                op.log.event(f"{kind} {name} finished: {op.status} in {duration:.2f}s")
            if self.history is not None and op.execution_id:
                self.history.finish(
                    op.execution_id,
                    str(op.status),
                    exit_code=op.exit_code,
                    duration=duration,
                    error=op.error,
                    metadata=op.metadata,
                )

    @staticmethod
    def _apply_exception(op: Operation, exc: BaseException) -> None:
        if isinstance(exc, KeyboardInterrupt | OperationCancelledError):
            op.status = Status.CANCELLED
            op.exit_code = 130
            op.error = "cancelled"
        elif isinstance(exc, TimeoutExpiredError):
            op.status = Status.TIMEOUT
            op.exit_code = int(exc.exit_code)
            op.error = exc.message
        elif isinstance(exc, CommandFailedError):
            op.status = Status.FAILED
            op.exit_code = exc.command_exit_code
            op.error = exc.message
        elif isinstance(exc, HighhXError):
            op.status = Status.FAILED
            op.exit_code = int(exc.exit_code)
            op.error = exc.message
        else:
            op.status = Status.FAILED
            op.exit_code = 1
            op.error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------ policy/approval
    def evaluate_policy(
        self,
        action: str,
        *,
        command: str | None = None,
        target: str | None = None,
        production: bool = False,
        host: str | None = None,
        app: str | None = None,
    ) -> PolicyDecision:
        """Evaluate policies; raises :class:`PolicyViolationError` on deny."""
        facts = self.facts()
        decision = self.policy.evaluate(
            PolicyContext(
                action=action,
                command=command,
                branch=facts.get("branch"),
                target=target,
                profile=facts.get("profile"),
                production=production,
                host=host,
                app=app,
            )
        )
        if decision.denied:
            raise PolicyViolationError(
                f"Blocked by project policy: {self.redactor.redact(command) if command else action}",
                details=decision.messages,
                hint="See .highhx/policies.yaml (run `highhx policy` to list rules).",
            )
        if decision.effect == Effect.WARN:
            for message in decision.messages:
                self.output.warn(f"policy: {message}")
        return decision

    def approve(
        self,
        action: str,
        risk: RiskLevel,
        *,
        details: Sequence[str] = (),
        identifiers: Sequence[str] = (),
        bypassable: bool = True,
        confirm_word: str = "yes",
        command: str | None = None,
        target: str | None = None,
        production: bool = False,
        policy_action: str | None = None,
    ) -> ApprovalDecision:
        """Policy check + approval for an action. Raises if denied."""
        decision = self.evaluate_policy(policy_action or action, command=command, target=target, production=production)
        if decision.effect == Effect.REQUIRE_APPROVAL:
            risk = max(risk, decision.risk)
            bypassable = bypassable and decision.bypassable
            details = [*details, *decision.messages]
        request = ApprovalRequest(
            action=action,
            risk=risk,
            details=list(details),
            identifiers=[*identifiers, *decision.rule_ids, *([policy_action] if policy_action else [])],
            bypassable=bypassable,
            confirm_word=confirm_word,
        )
        self.ctx.events.emit(APPROVAL_REQUESTED, action=action, risk=risk.label)
        try:
            result = self.approvals.require(request)
        except HighhXError:
            self.ctx.events.emit(APPROVAL_DECIDED, action=action, approved=False, mode="denied")
            raise
        self.ctx.events.emit(APPROVAL_DECIDED, action=action, approved=True, mode=result.mode)
        return result

    # ------------------------------------------------------------- commands
    def _prepare(self, spec: CommandSpec) -> CommandSpec:
        # Isolated commands (env_base set, e.g. plugin commands) must not receive the
        # project's environment profile — that is where secrets live.
        if self.ctx.env and spec.env_base is None:
            merged = {**self.ctx.env, **spec.env}
            spec = dataclasses.replace(spec, env=merged)
        if spec.cwd is None:
            spec = dataclasses.replace(spec, cwd=self.ctx.cwd)
        self.redactor.add_environment(spec.env)
        return spec

    def run(
        self,
        spec: CommandSpec,
        *,
        action: str | None = None,
        risk: RiskLevel | None = None,
        check: bool = False,
        echo: bool = True,
        source: str | None = None,
        approved: bool = False,
        record: bool = True,
        target: str | None = None,
        production: bool = False,
        on_line: Callable[[str, str], None] | None = None,
        policy_action: str | None = None,
        cancel: CancellationToken | None = None,
    ) -> CommandResult:
        """Run a side-effecting command with full safety checks.

        ``policy_action`` names the action for policy rules (e.g. ``git:push``);
        it defaults to ``exec:<program>``.
        """
        spec = self._prepare(spec)
        display = spec.display()
        classification = classify_command(display, self.approvals.policy.rules)
        final_risk = max(risk, classification.risk) if risk is not None else classification.risk
        if approved:
            self.evaluate_policy(
                policy_action or f"exec:{spec.program()}", command=display, target=target, production=production
            )
        else:
            self.approve(
                action or f"Run `{self.redactor.redact(display)}`",
                final_risk,
                details=classification.reasons,
                identifiers=classification.rule_ids,
                bypassable=classification.bypassable,
                command=display,
                target=target,
                production=production,
                policy_action=policy_action or f"exec:{spec.program()}",
            )
        if self.ctx.options.dry_run:
            self.output.info(f"[dry-run] would run: {self.redactor.redact(display)}")
            return CommandResult(display, None, Status.SKIPPED, dry_run=True, error="dry run")

        self.output.detail(f"$ {self.redactor.redact(display)}")
        op = _current_operation.get()
        own_op = op is None and record and self.history is not None
        if own_op:
            with self.operation("command", spec.name or spec.program(), command=display):
                result = self._execute(spec, echo=echo, source=source, on_line=on_line, cancel=cancel)
                inner = _current_operation.get()
                if inner is not None and not result.ok:
                    inner.status = result.status
                    inner.exit_code = result.exit_code
                    inner.error = result.error or f"exit code {result.exit_code}"
        else:
            result = self._execute(spec, echo=echo, source=source, on_line=on_line, cancel=cancel)
            if op is not None and record and self.history is not None and op.execution_id:
                self.history.add_step(
                    op.execution_id,
                    spec.name or spec.program(),
                    str(result.status),
                    command=display,
                    exit_code=result.exit_code,
                    started_at=result.started_at,
                    duration=result.duration,
                    error=result.error,
                )
        # Returned output is user-facing (e.g. --json); never hand back secret values.
        result = dataclasses.replace(
            result, stdout=self.redactor.redact(result.stdout), stderr=self.redactor.redact(result.stderr)
        )
        if check:
            self.raise_for(result)
        return result

    def _execute(
        self,
        spec: CommandSpec,
        *,
        echo: bool,
        source: str | None,
        on_line: Callable[[str, str], None] | None,
        cancel: CancellationToken | None = None,
    ) -> CommandResult:
        op = _current_operation.get()
        writer = op.log if op is not None else None
        if writer is not None:
            writer.event(f"$ {spec.display()}")

        def _line(stream: str, line: str) -> None:
            clean = self.redactor.redact(line)
            if writer is not None:
                writer.write(clean, stream=stream, source=source)
            if echo:
                self.output.stream_line(clean, stream=stream, source=source)
            if on_line is not None:
                on_line(stream, line)

        return self.executor.execute(spec, cancel=cancel or self.ctx.cancel, on_line=_line)

    def capture(self, spec: CommandSpec) -> CommandResult:
        """Run a read-only query command silently (no approval, runs in dry-run too)."""
        spec = self._prepare(spec)
        return self.executor.execute(spec, cancel=self.ctx.cancel)

    @staticmethod
    def raise_for(result: CommandResult, *, hint: str | None = None) -> None:
        """Raise the appropriate error for an unsuccessful result."""
        if result.ok or result.dry_run:
            return
        if result.status == Status.TIMEOUT:
            raise TimeoutExpiredError(f"Command timed out: {result.command}", hint=hint)
        if result.status == Status.CANCELLED:
            raise OperationCancelledError(f"Command cancelled: {result.command}")
        if result.exit_code == 127 or (result.error or "").startswith("command not found"):
            raise CommandFailedError(
                result.command, 127, hint=hint or "Is the tool installed and on PATH? Try `highhx doctor`."
            )
        raise CommandFailedError(
            result.command, result.exit_code if result.exit_code is not None else 1, hint=hint, stderr=result.stderr
        )
