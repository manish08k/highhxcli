"""Dependency-aware parallel scheduler.

The scheduler never starts a node before all of its dependencies have finished,
runs up to ``max_parallel`` nodes concurrently, and supports fail-fast
cancellation and graceful interruption (Ctrl+C).
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from highhx.core.result import Status, StepResult
from highhx.execution.cancellation import CancellationToken
from highhx.workflows.dependency_graph import DependencyGraph

log = logging.getLogger(__name__)

SkipDecision = Callable[[str, dict[str, StepResult]], str | None]
"""Returns a skip reason, or None to run the node. May raise to fail the node."""

NodeRunner = Callable[[str, CancellationToken], StepResult]


@dataclass
class SchedulerHooks:
    decide: SkipDecision
    execute: NodeRunner
    on_start: Callable[[str], None] | None = None
    on_finish: Callable[[StepResult], None] | None = None


def _failed(result: StepResult) -> bool:
    return result.status in (Status.FAILED, Status.TIMEOUT) and not result.allowed_failure


class DagScheduler:
    """Runs graph nodes respecting dependencies and parallelism limits."""

    def __init__(
        self,
        graph: DependencyGraph,
        *,
        max_parallel: int = 4,
        fail_fast: bool = True,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.graph = graph
        self.max_parallel = max(1, max_parallel)
        self.fail_fast = fail_fast
        self.cancel = cancel or CancellationToken()
        self.results: dict[str, StepResult] = {}
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}
        self.interrupted = False
        self.max_observed_parallel = 0

    def snapshot(self) -> dict[str, StepResult]:
        with self._lock:
            return dict(self.results)

    def _record(self, result: StepResult, hooks: SchedulerHooks) -> None:
        with self._lock:
            self.results[result.step_id] = result
        if hooks.on_finish is not None:
            try:
                hooks.on_finish(result)
            except Exception:  # pragma: no cover - reporter errors must not break scheduling
                log.exception("on_finish hook failed")
        if self.fail_fast and _failed(result):
            for node, token in list(self._tokens.items()):
                if node != result.step_id:
                    token.cancel(f"fail-fast: step '{result.step_id}' failed")

    def _run_node(self, node: str, token: CancellationToken, hooks: SchedulerHooks) -> StepResult:
        began = time.monotonic()
        try:
            result = hooks.execute(node, token)
        except BaseException as exc:
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            status = Status.CANCELLED if isinstance(exc, KeyboardInterrupt) else Status.FAILED
            result = StepResult(node, status, message=str(message))
        if not result.duration:
            result.duration = time.monotonic() - began
        return result

    def run(self, hooks: SchedulerHooks) -> dict[str, StepResult]:
        order = self.graph.topological_order()
        started: set[str] = set()
        running: dict[Future[StepResult], str] = {}
        pool = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="highhx-step")
        try:
            while True:
                try:
                    self._schedule(order, started, running, pool, hooks)
                    if not running:
                        break
                    done, _ = wait(list(running), timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        node = running.pop(future)
                        self._tokens.pop(node, None)
                        self._record(future.result(), hooks)
                except KeyboardInterrupt:
                    self.interrupted = True
                    self.cancel.cancel("interrupted")
        finally:
            pool.shutdown(wait=True)
        for node in order:
            if node not in self.results:
                self._record(StepResult(node, Status.CANCELLED, message="not started (workflow cancelled)"), hooks)
        return self.snapshot()

    def _schedule(
        self,
        order: list[str],
        started: set[str],
        running: dict[Future[StepResult], str],
        pool: ThreadPoolExecutor,
        hooks: SchedulerHooks,
    ) -> None:
        progressed = True
        while progressed:
            progressed = False
            if self.cancel.cancelled:
                for node in order:
                    if node not in started:
                        started.add(node)
                        why = self.cancel.reason or "cancelled"
                        self._record(StepResult(node, Status.CANCELLED, message=f"not started ({why})"), hooks)
                return
            for node in order:
                if node in started:
                    continue
                if not all(dep in self.results for dep in self.graph.dependencies(node) if dep in self.graph):
                    continue
                try:
                    reason = hooks.decide(node, self.snapshot())
                except Exception as exc:
                    started.add(node)
                    self._record(StepResult(node, Status.FAILED, message=getattr(exc, "message", str(exc))), hooks)
                    progressed = True
                    continue
                if reason is not None:
                    started.add(node)
                    self._record(StepResult(node, Status.SKIPPED, message=reason), hooks)
                    progressed = True
                    continue
                if len(running) >= self.max_parallel:
                    return
                started.add(node)
                token = self.cancel.child()
                self._tokens[node] = token
                if hooks.on_start is not None:
                    hooks.on_start(node)
                # Run in a copy of the caller's context so the current operation (history
                # entry, log file) and tracing span are visible inside worker threads.
                context = contextvars.copy_context()
                running[pool.submit(context.run, self._run_node, node, token, hooks)] = node
                self.max_observed_parallel = max(self.max_observed_parallel, len(running))
                progressed = True
