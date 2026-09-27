"""Workflow execution."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import HighhXError, OperationCancelledError, WorkflowError
from highhx.core.events import STEP_FINISHED, STEP_STARTED, WORKFLOW_FINISHED, WORKFLOW_STARTED
from highhx.core.result import CommandResult, Status, StepResult, WorkflowResult
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec
from highhx.utils.hashing import new_id
from highhx.utils.platform import system_name
from highhx.workflows import running
from highhx.workflows.conditions import EvalContext, ExpressionError, evaluate_condition
from highhx.workflows.dependency_graph import DependencyGraph
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.resolver import resolve_inputs, reusable_workflows
from highhx.workflows.schema import StepSpec, WorkflowSpec
from highhx.workflows.validator import validate_spec
from highhx.workflows.variables import interpolate, interpolate_mapping, try_interpolate

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor, Planned
    from highhx.actions.spec import ActionResult

MAX_NESTING = 8


class WorkflowReporter(Protocol):
    """Presentation hooks (implemented by the CLI)."""

    def workflow_started(self, spec: WorkflowSpec, stages: list[list[str]], depth: int) -> None: ...

    def step_started(self, spec: WorkflowSpec, step: StepSpec, depth: int) -> None: ...

    def step_finished(self, spec: WorkflowSpec, result: StepResult, depth: int) -> None: ...

    def workflow_finished(self, result: WorkflowResult, depth: int) -> None: ...

    def plan(self, spec: WorkflowSpec, stages: list[list[str]], commands: dict[str, list[str]]) -> None: ...


class NullReporter:
    def workflow_started(self, spec: WorkflowSpec, stages: list[list[str]], depth: int) -> None:
        return None

    def step_started(self, spec: WorkflowSpec, step: StepSpec, depth: int) -> None:
        return None

    def step_finished(self, spec: WorkflowSpec, result: StepResult, depth: int) -> None:
        return None

    def workflow_finished(self, result: WorkflowResult, depth: int) -> None:
        return None

    def plan(self, spec: WorkflowSpec, stages: list[list[str]], commands: dict[str, list[str]]) -> None:
        return None


def parse_outputs_file(path: Path) -> dict[str, str]:
    """Parse ``key=value`` lines (and ``key<<DELIM`` heredocs) written by a step."""
    if not path.exists():
        return {}
    outputs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key, delimiter = line.split("<<", 1)
            collected: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                collected.append(lines[index])
                index += 1
            index += 1
            outputs[key.strip()] = "\n".join(collected)
        elif "=" in line:
            key, value = line.split("=", 1)
            if key.strip():
                outputs[key.strip()] = value
    return outputs


class WorkflowEngine:
    """Validates and executes workflows through the core :class:`Engine`."""

    def __init__(
        self,
        engine: Engine,
        loader: WorkflowLoader,
        *,
        root: Path,
        reporter: WorkflowReporter | None = None,
        project_name: str | None = None,
        actions: Callable[[], ActionExecutor] | None = None,
    ) -> None:
        self.engine = engine
        self.loader = loader
        self.root = root
        self.reporter: WorkflowReporter = reporter or NullReporter()
        self.project_name = project_name
        self._actions_factory = actions
        self._actions: ActionExecutor | None = None
        self._lock = threading.Lock()

    @property
    def actions(self) -> ActionExecutor:
        """The action executor for `action:` steps (created on first use)."""
        if self._actions is None:
            if self._actions_factory is None:
                raise WorkflowError("This workflow engine cannot run action steps (no action executor).")
            self._actions = self._actions_factory()
        return self._actions

    # ---------------------------------------------------------------- setup
    def load(self, workflow: str | WorkflowSpec) -> WorkflowSpec:
        return workflow if isinstance(workflow, WorkflowSpec) else self.loader.load(workflow)

    def prepare(self, spec: WorkflowSpec) -> DependencyGraph:
        """Validate and build the dependency graph (raises on any error)."""
        report = validate_spec(spec, loader=self.loader, check_tools=False, base_dir=self.root)
        if report.errors:
            raise WorkflowError(
                f"Workflow '{spec.name}' is invalid",
                details=report.errors,
                hint="Run `highhx workflow validate` for details.",
            )
        graph = DependencyGraph()
        for step in spec.steps:
            graph.add_node(step.id)
            for dep in step.depends_on:
                graph.add_edge(step.id, dep)
        return graph

    def _base_context(
        self,
        spec: WorkflowSpec,
        inputs: Mapping[str, Any],
        results: Mapping[str, StepResult],
        execution_id: str,
        extra_env: Mapping[str, str] | None = None,
        status: Mapping[str, bool] | None = None,
        strict: bool = True,
    ) -> EvalContext:
        env = {**os.environ, **self.engine.ctx.env, **(extra_env or {})}
        data: dict[str, Any] = {
            "env": env,
            "vars": spec.vars,
            "inputs": dict(inputs),
            "steps": {
                sid: {"status": str(r.status), "outputs": r.outputs, "exit_code": r.exit_code}
                for sid, r in results.items()
            },
            "workflow": {"name": spec.name, "key": spec.key},
            "execution": {"id": execution_id},
            "platform": system_name(),
            "project": {"root": str(self.root), "name": self.project_name},
        }
        return EvalContext(
            data=data,
            status=status or {"success": True, "failure": False, "cancelled": False},
            base_dir=self.root,
            strict=strict,
        )

    def _workflow_env(self, spec: WorkflowSpec, inputs: Mapping[str, Any], execution_id: str) -> dict[str, str]:
        ctx = self._base_context(spec, inputs, {}, execution_id)
        env = interpolate_mapping(spec.env, ctx)
        env.update(
            {
                "HIGHHX_WORKFLOW": spec.key or spec.name,
                "HIGHHX_EXECUTION_ID": execution_id,
                "HIGHHX_PROJECT_ROOT": str(self.root),
            }
        )
        return env

    def _resolve_cwd(self, spec: WorkflowSpec, step: StepSpec, ctx: EvalContext) -> Path:
        base = self.root
        if spec.settings.working_directory:
            base = (self.root / interpolate(spec.settings.working_directory, ctx)).resolve()
        if step.cwd:
            return (base / interpolate(step.cwd, ctx)).resolve()
        return base

    # ------------------------------------------------------------------ run
    def run(
        self,
        workflow: str | WorkflowSpec,
        *,
        inputs: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        cancel: CancellationToken | None = None,
        resume: Mapping[str, StepResult] | None = None,
        _depth: int = 0,
    ) -> WorkflowResult:
        """Run a workflow to completion and return its result.

        Raises :class:`WorkflowError` for invalid workflows and
        :class:`OperationCancelledError` if interrupted (after recording history).
        ``resume`` maps steps that already succeeded in an earlier run to their results: they are
        not run again (their outputs are reused) — see :meth:`resume_state`.
        """
        if _depth > MAX_NESTING:
            raise WorkflowError("Workflows are nested too deeply (possible recursion).")
        spec = self.load(workflow)
        graph = self.prepare(spec)
        resolved_inputs = resolve_inputs(spec, inputs or {})
        reusable_workflows(spec, self.loader)
        if self.engine.dry_run:
            return self._dry_run(spec, graph, resolved_inputs)

        metadata: dict[str, Any] = {"workflow": spec.name, "key": spec.key, "inputs": dict(resolved_inputs)}
        if resume:
            metadata["resumed_steps"] = sorted(resume)
        completed_actions: list[tuple[str, Planned, ActionResult]] = []
        finish_order: list[str] = []
        with self.engine.operation("workflow", spec.key or spec.name, metadata=metadata) as op:
            execution_id = op.execution_id or new_id()
            registration = (
                running.registered(execution_id, spec.key or spec.name, self.root)
                if _depth == 0
                else contextlib.nullcontext()
            )
            registration.__enter__()
            parent_token = cancel or self.engine.ctx.cancel
            token = parent_token.child()
            timed_out = threading.Event()
            timer: threading.Timer | None = None
            if spec.settings.timeout:

                def _expire() -> None:
                    timed_out.set()
                    token.cancel(f"workflow timeout after {spec.settings.timeout:g}s")

                timer = threading.Timer(spec.settings.timeout, _expire)
                timer.daemon = True
                timer.start()
            workflow_env = {**self._workflow_env(spec, resolved_inputs, execution_id), **(env or {})}
            stages = graph.levels()
            self.engine.ctx.events.emit(WORKFLOW_STARTED, workflow=spec.name, execution_id=execution_id)
            self.reporter.workflow_started(spec, stages, _depth)
            began = time.monotonic()
            failed_flag = threading.Event()

            from highhx.workflows.scheduler import DagScheduler, SchedulerHooks

            scheduler = DagScheduler(
                graph, max_parallel=spec.settings.max_parallel, fail_fast=spec.settings.fail_fast, cancel=token
            )

            def decide(node: str, results: dict[str, StepResult]) -> str | None:
                step = spec.step(node)
                ancestors = graph.ancestors(node)
                deps_ok = all(results[d].effective_success for d in step.depends_on)
                ancestor_failed = any(
                    a in results
                    and results[a].status in (Status.FAILED, Status.TIMEOUT, Status.CANCELLED)
                    and not results[a].allowed_failure
                    for a in ancestors
                )
                workflow_failed = failed_flag.is_set()
                status = {
                    "success": deps_ok and not (spec.settings.fail_fast and workflow_failed) and not token.cancelled,
                    "failure": ancestor_failed or (not step.depends_on and workflow_failed),
                    "cancelled": token.cancelled,
                }
                ctx = self._base_context(spec, resolved_inputs, results, execution_id, workflow_env, status)
                try:
                    run = evaluate_condition(step.condition, ctx)
                except ExpressionError as exc:
                    raise WorkflowError(f"condition error in step '{node}': {exc}") from exc
                if run:
                    return None
                if not step.condition:
                    if not deps_ok:
                        return "skipped: a dependency did not succeed"
                    if token.cancelled:
                        return f"skipped: {token.reason or 'cancelled'}"
                    return "skipped: an earlier step failed (fail_fast)"
                return f"skipped: condition '{step.condition}' is false"

            def execute(node: str, step_token: CancellationToken) -> StepResult:
                if resume and node in resume and resume[node].status == Status.SUCCESS:
                    earlier = resume[node]
                    return StepResult(
                        node, Status.SUCCESS, outputs=earlier.outputs, message="already completed (resumed)"
                    )
                return self._execute_step(
                    spec,
                    spec.step(node),
                    step_token,
                    scheduler,
                    resolved_inputs,
                    workflow_env,
                    execution_id,
                    _depth,
                    completed_actions,
                )

            def on_start(node: str) -> None:
                self.engine.ctx.events.emit(STEP_STARTED, workflow=spec.name, step=node)
                self.reporter.step_started(spec, spec.step(node), _depth)

            def on_finish(result: StepResult) -> None:
                with self._lock:
                    finish_order.append(result.step_id)
                if result.status in (Status.FAILED, Status.TIMEOUT) and not result.allowed_failure:
                    failed_flag.set()
                self.engine.ctx.events.emit(
                    STEP_FINISHED, workflow=spec.name, step=result.step_id, status=str(result.status)
                )
                self.reporter.step_finished(spec, result, _depth)

            try:
                results = scheduler.run(
                    SchedulerHooks(decide=decide, execute=execute, on_start=on_start, on_finish=on_finish)
                )
            finally:
                if timer is not None:
                    timer.cancel()
                registration.__exit__(None, None, None)

            ordered = {sid: results[sid] for sid in spec.step_ids}
            if timed_out.is_set():
                status = Status.TIMEOUT
            elif scheduler.interrupted or (
                parent_token.cancelled and any(r.status == Status.CANCELLED for r in ordered.values())
            ):
                status = Status.CANCELLED
            elif any(
                r.status in (Status.FAILED, Status.TIMEOUT, Status.CANCELLED) and not r.allowed_failure
                for r in ordered.values()
            ):
                status = Status.FAILED
            else:
                status = Status.SUCCESS

            rollback: list[dict[str, Any]] = []
            if status in (Status.FAILED, Status.TIMEOUT) and spec.on_failure == "rollback":
                rollback = self._rollback(
                    spec, ordered, finish_order, completed_actions, workflow_env, resolved_inputs, execution_id
                )
                op.metadata["rollback"] = rollback

            out_ctx = self._base_context(spec, resolved_inputs, ordered, execution_id, workflow_env, strict=False)
            outputs: dict[str, str] = {}
            for key, expr in spec.outputs.items():
                try:
                    outputs[key] = interpolate(expr, out_ctx)
                except ExpressionError:
                    outputs[key] = ""
            result = WorkflowResult(
                workflow=spec.key or spec.name,
                execution_id=execution_id,
                status=status,
                steps=ordered,
                duration=time.monotonic() - began,
                outputs=outputs,
                rollback=rollback,
            )
            self._record(op.execution_id, result)
            op.metadata.update(
                {"steps": len(ordered), "failed_steps": [s for s, r in ordered.items() if r.status == Status.FAILED]}
            )
            if status != Status.SUCCESS:
                op.status = status
                op.exit_code = 130 if status == Status.CANCELLED else 124 if status == Status.TIMEOUT else 1
                failed = [s for s, r in ordered.items() if r.status in (Status.FAILED, Status.TIMEOUT)]
                op.error = f"workflow {status}" + (f": failed steps {', '.join(failed)}" if failed else "")
            self.engine.ctx.events.emit(WORKFLOW_FINISHED, workflow=spec.name, status=str(status))
            self.reporter.workflow_finished(result, _depth)
            if status == Status.CANCELLED and scheduler.interrupted and _depth == 0:
                raise OperationCancelledError(f"Workflow '{spec.name}' was interrupted.")
            return result

    def _record(self, execution_id: str | None, result: WorkflowResult) -> None:
        history = self.engine.history
        if history is None or not execution_id:
            return
        for step in result.steps.values():
            history.add_step(
                execution_id,
                step.step_id,
                str(step.status),
                command=" && ".join(c.command for c in step.commands) or None,
                exit_code=step.exit_code,
                duration=step.duration,
                error=step.message or None,
                outputs=step.outputs,
            )
        tracer = self.engine.tracer
        if tracer is not None:
            for step in result.steps.values():
                tracer.record(f"step:{step.step_id}", step.duration, status=str(step.status), workflow=result.workflow)

    def _execute_step(
        self,
        spec: WorkflowSpec,
        step: StepSpec,
        token: CancellationToken,
        scheduler: Any,
        inputs: Mapping[str, Any],
        workflow_env: Mapping[str, str],
        execution_id: str,
        depth: int,
        completed_actions: list[tuple[str, Planned, ActionResult]] | None = None,
    ) -> StepResult:
        began = time.monotonic()
        results = scheduler.snapshot()
        ctx = self._base_context(spec, inputs, results, execution_id, workflow_env)
        try:
            step_env = {**workflow_env, **interpolate_mapping(step.env, ctx)}
            ctx = self._base_context(spec, inputs, results, execution_id, step_env)
            cwd = self._resolve_cwd(spec, step, ctx)
        except ExpressionError as exc:
            return StepResult(
                step.id, Status.FAILED, message=f"variable error: {exc}", duration=time.monotonic() - began
            )

        approved = False
        if step.approval is not None:
            details = [step.approval.message] if step.approval.message else []
            details += [try_interpolate(c, ctx) for c in step.run] or [f"runs workflow '{step.uses}'"]
            self.engine.approve(
                f"Run step '{step.label}' of workflow '{spec.name}'",
                step.approval.risk,
                details=details,
                bypassable=step.approval.bypassable,
                policy_action=f"workflow:{spec.key}:{step.id}",
            )
            approved = True

        if step.action:
            return self._action_step(step, token, ctx, began, completed_actions)

        if step.uses:
            try:
                child_inputs = {k: interpolate(str(v), ctx) for k, v in step.with_.items()}
            except ExpressionError as exc:
                return StepResult(step.id, Status.FAILED, message=f"variable error: {exc}")
            child = self.run(step.uses, inputs=child_inputs, env=step_env, cancel=token, _depth=depth + 1)
            child_status = child.status if child.status != Status.SKIPPED else Status.SUCCESS
            return StepResult(
                step.id,
                child_status,
                outputs=child.outputs,
                message=f"workflow '{step.uses}' {child.status}",
                duration=time.monotonic() - began,
                allowed_failure=step.continue_on_error and child_status in (Status.FAILED, Status.TIMEOUT),
            )

        commands: list[CommandResult] = []
        fd, outputs_name = tempfile.mkstemp(prefix="highhx-output-", suffix=".txt")
        os.close(fd)
        outputs_path = Path(outputs_name)
        try:
            env = {
                **step_env,
                "HIGHHX_STEP_ID": step.id,
                "HIGHHX_OUTPUT": str(outputs_path),
            }
            for raw in step.run:
                try:
                    text = interpolate(raw, ctx)
                except ExpressionError as exc:
                    return StepResult(
                        step.id,
                        Status.FAILED,
                        commands,
                        message=f"variable error: {exc}",
                        duration=time.monotonic() - began,
                    )
                command = CommandSpec(
                    text,
                    cwd=cwd,
                    env=env,
                    timeout=step.timeout,
                    retry=step.retry,
                    shell=step.shell,  # nosec B604 - the user's own workflow `shell` setting; steps are risk-classified and gated
                    name=step.id,
                )
                result = self.engine.run(
                    command,
                    action=f"Run `{text}` (step '{step.id}' of workflow '{spec.name}')",
                    approved=approved,
                    record=False,
                    source=step.id,
                    cancel=token,
                    policy_action=f"workflow:{spec.key}:{step.id}",
                )
                commands.append(result)
                if not result.ok:
                    break
            outputs = parse_outputs_file(outputs_path)
        finally:
            outputs_path.unlink(missing_ok=True)

        last = commands[-1] if commands else None
        if last is None or all(c.ok for c in commands):
            status, message = Status.SUCCESS, ""
        else:
            status = last.status
            message = last.error or f"exit code {last.exit_code}"
        return StepResult(
            step.id,
            status,
            commands,
            outputs=outputs,
            message=message,
            duration=time.monotonic() - began,
            allowed_failure=step.continue_on_error and status in (Status.FAILED, Status.TIMEOUT),
        )

    def _action_inputs(self, values: Mapping[str, Any], ctx: EvalContext) -> dict[str, Any]:
        def expand(value: Any) -> Any:
            if isinstance(value, str):
                return interpolate(value, ctx)
            if isinstance(value, list):
                return [expand(v) for v in value]
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            return value

        return {key: expand(value) for key, value in values.items()}

    def _action_step(
        self,
        step: StepSpec,
        token: CancellationToken,
        ctx: EvalContext,
        began: float,
        completed_actions: list[tuple[str, Planned, ActionResult]] | None,
    ) -> StepResult:
        assert step.action is not None
        try:
            inputs = self._action_inputs(step.with_, ctx)
        except ExpressionError as exc:
            return StepResult(step.id, Status.FAILED, message=f"variable error: {exc}")
        try:
            planned = self.actions.plan(step.action, inputs)
        except HighhXError as exc:
            detail = "; ".join(exc.details[:3])
            return StepResult(step.id, Status.FAILED, message=exc.message + (f": {detail}" if detail else ""))
        result = self.actions.execute(planned, cancel=token)
        if result.ok and completed_actions is not None:
            with self._lock:
                completed_actions.append((step.id, planned, result))
        status = {
            "ok": Status.SUCCESS,
            "cancelled": Status.CANCELLED,
            "timeout": Status.TIMEOUT,
        }.get(result.status, Status.SUCCESS if result.ok else Status.FAILED)
        outputs = {k: v if isinstance(v, str) else json.dumps(v, default=str) for k, v in result.output.items()}
        return StepResult(
            step.id,
            status,
            outputs=outputs,
            message=result.summary if result.ok else (result.error or result.summary or result.status),
            duration=time.monotonic() - began,
            allowed_failure=step.continue_on_error and status in (Status.FAILED, Status.TIMEOUT),
        )

    def _rollback(
        self,
        spec: WorkflowSpec,
        results: Mapping[str, StepResult],
        finish_order: list[str],
        completed_actions: list[tuple[str, Planned, ActionResult]],
        workflow_env: Mapping[str, str],
        inputs: Mapping[str, Any],
        execution_id: str,
    ) -> list[dict[str, Any]]:
        """Undo the steps that completed, newest first (on_failure: rollback). Every undo goes through
        the same classification and approval as any other action; a failing undo is reported and the
        rollback continues with the next step."""
        actions = {step_id: (planned, result) for step_id, planned, result in completed_actions}
        order = [s for s in reversed(finish_order) if results.get(s) and results[s].status == Status.SUCCESS]
        report: list[dict[str, Any]] = []
        ctx = self._base_context(spec, inputs, results, execution_id, workflow_env, strict=False)
        self.engine.ctx.events.emit("workflow.rollback", workflow=spec.name, steps=len(order))
        for step_id in order:
            step = spec.step(step_id)
            undo = step.rollback
            if undo is None:
                continue
            entry: dict[str, Any] = {"step": step_id}
            try:
                if undo.compensate and step_id in actions:
                    planned, result = actions[step_id]
                    done = self.actions.compensate(planned, result) or "nothing to undo"
                    entry.update(ok=not done.startswith("compensation failed"), detail=done)
                elif undo.action:
                    outcome = self.actions.run(undo.action, self._action_inputs(undo.with_, ctx))
                    entry.update(ok=outcome.ok, detail=outcome.summary or outcome.error)
                else:
                    ok = True
                    for raw in undo.run:
                        command = interpolate(raw, ctx)
                        outcome_cmd = self.engine.run(
                            CommandSpec(command, cwd=self.root, env=dict(workflow_env), name=f"{step_id}-rollback"),
                            action=f"Undo step '{step_id}' of workflow '{spec.name}': `{command}`",
                            policy_action=f"workflow:{spec.key}:{step_id}:rollback",
                            record=False,
                        )
                        ok = ok and outcome_cmd.ok
                        if not outcome_cmd.ok:
                            break
                    entry.update(ok=ok, detail=" && ".join(undo.run))
            except (HighhXError, ExpressionError) as exc:
                entry.update(ok=False, detail=str(getattr(exc, "message", exc)))
            report.append(entry)
        return report

    # ---------------------------------------------------------------- resume
    def resume_state(self, execution_id: str) -> tuple[str, dict[str, Any], dict[str, StepResult]]:
        """``(workflow, inputs, completed steps)`` of a recorded run, for :meth:`run` (``resume=``)."""
        history = self.engine.history
        if history is None:
            raise WorkflowError("Workflow history is unavailable (run `highhx init` to enable it).")
        record = history.get(execution_id)
        if record.kind != "workflow":
            raise WorkflowError(f"{record.id} is a {record.kind}, not a workflow run.")
        if record.status in ("success", "running"):
            raise WorkflowError(
                f"Workflow run {record.id} is {record.status}; only failed, cancelled or timed-out runs can be resumed."
            )
        key = str(record.metadata.get("key") or record.name)
        inputs = dict(record.metadata.get("inputs") or {})
        undone = {str(r.get("step")) for r in record.metadata.get("rollback") or [] if r.get("ok")}
        completed = {
            step.step_id: StepResult(step.step_id, Status.SUCCESS, outputs=dict(step.outputs or {}))
            for step in record.steps
            if step.status == "success" and ":" not in step.step_id and step.step_id not in undone
        }
        return key, inputs, completed

    # -------------------------------------------------------------- dry run
    def plan_commands(self, spec: WorkflowSpec, inputs: Mapping[str, Any]) -> dict[str, list[str]]:
        ctx = self._base_context(spec, inputs, {}, "dry-run", strict=False)
        return {
            step.id: [try_interpolate(c, ctx) for c in step.run] if step.run else [f"(workflow) {step.uses}"]
            for step in spec.steps
        }

    def _dry_run(self, spec: WorkflowSpec, graph: DependencyGraph, inputs: Mapping[str, Any]) -> WorkflowResult:
        stages = graph.levels()
        commands = self.plan_commands(spec, inputs)
        self.reporter.plan(spec, stages, commands)
        steps = {step.id: StepResult(step.id, Status.SKIPPED, message="dry run") for step in spec.steps}
        return WorkflowResult(spec.key or spec.name, "dry-run", Status.SKIPPED, steps, dry_run=True)

    def plan(self, workflow: str | WorkflowSpec) -> list[list[str]]:
        spec = self.load(workflow)
        return self.prepare(spec).levels()

    @staticmethod
    def step_risk(step: StepSpec) -> RiskLevel:
        if step.approval is not None:
            return step.approval.risk
        from highhx.approvals.risk import classify_command

        return max((classify_command(c).risk for c in step.run), default=RiskLevel.NORMAL)
