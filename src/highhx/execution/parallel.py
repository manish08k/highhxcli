"""Bounded parallel execution of independent callables."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Generic, TypeVar

from highhx.execution.cancellation import CancellationToken

T = TypeVar("T")


def default_parallelism() -> int:
    """Reasonable default worker count."""
    return max(1, min(8, os.cpu_count() or 2))


@dataclass
class TaskOutcome(Generic[T]):
    """Result of one parallel task: either ``value`` or ``error`` is set."""

    index: int
    value: T | None = None
    error: BaseException | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


def run_parallel(
    tasks: Sequence[Callable[[], T]],
    *,
    max_workers: int | None = None,
    cancel: CancellationToken | None = None,
    stop_when: Callable[[T], bool] | None = None,
) -> list[TaskOutcome[T]]:
    """Run ``tasks`` with at most ``max_workers`` concurrently.

    Results are returned in input order. If ``stop_when(value)`` is true for a
    finished task (e.g. a failure with fail-fast) or ``cancel`` fires, tasks
    that have not started yet are marked skipped.
    """
    outcomes: list[TaskOutcome[T]] = [TaskOutcome(index=i) for i in range(len(tasks))]
    if not tasks:
        return outcomes
    workers = max(1, max_workers or default_parallelism())
    next_index = 0
    stop = False
    running: dict[Future[T], int] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="highhx") as pool:
        while next_index < len(tasks) or running:
            while not stop and next_index < len(tasks) and len(running) < workers:
                if cancel is not None and cancel.cancelled:
                    stop = True
                    break
                running[pool.submit(tasks[next_index])] = next_index
                next_index += 1
            if stop:
                for index in range(next_index, len(tasks)):
                    outcomes[index].skipped = True
                next_index = len(tasks)
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                index = running.pop(future)
                try:
                    value = future.result()
                    outcomes[index].value = value
                    if stop_when is not None and stop_when(value):
                        stop = True
                except BaseException as exc:
                    outcomes[index].error = exc
                    if stop_when is not None:
                        stop = True
    return outcomes
