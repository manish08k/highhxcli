"""The command executor: retries, backoff, timeouts, cancellation and parallelism.

The executor is intentionally unaware of approvals, policies, history and UI —
those concerns live in :mod:`highhx.core.engine`, which wraps the executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from highhx.core.events import COMMAND_FINISHED, COMMAND_RETRY, COMMAND_STARTED, EventBus
from highhx.core.result import CommandResult, Status
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec
from highhx.execution.environment import build_environment
from highhx.execution.process import LineCallback, ProcessOutcome, run_process
from highhx.utils.time import iso_now

NON_RETRYABLE_EXIT_CODES = frozenset({126, 127})

ProcessRunner = Callable[..., ProcessOutcome]


def _status_for(outcome: ProcessOutcome) -> Status:
    if outcome.cancelled:
        return Status.CANCELLED
    if outcome.timed_out:
        return Status.TIMEOUT
    if outcome.exit_code == 0 and outcome.error is None:
        return Status.SUCCESS
    return Status.FAILED


class Executor:
    """Runs :class:`CommandSpec` objects and produces :class:`CommandResult` objects."""

    def __init__(
        self,
        events: EventBus | None = None,
        *,
        runner: ProcessRunner = run_process,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.events = events or EventBus()
        self._runner = runner
        self._sleeper = sleeper

    def _sleep(self, seconds: float, cancel: CancellationToken | None) -> bool:
        """Sleep, returning True if cancelled while waiting."""
        if seconds <= 0:
            return bool(cancel and cancel.cancelled)
        if self._sleeper is not None:
            self._sleeper(seconds)
            return bool(cancel and cancel.cancelled)
        if cancel is not None:
            return cancel.wait(seconds)
        time.sleep(seconds)
        return False

    def execute(
        self,
        spec: CommandSpec,
        *,
        cancel: CancellationToken | None = None,
        on_line: LineCallback | None = None,
    ) -> CommandResult:
        """Execute ``spec`` honouring its retry policy and timeout."""
        display = spec.display()
        started_at = iso_now()
        began = time.monotonic()
        try:
            argv = spec.argv()
        except ValueError as exc:
            return CommandResult(display, None, Status.FAILED, error=f"invalid command: {exc}", started_at=started_at)
        if spec.cwd is not None and not spec.cwd.is_dir():
            return CommandResult(
                display,
                None,
                Status.FAILED,
                error=f"working directory does not exist: {spec.cwd}",
                started_at=started_at,
            )
        if cancel is not None and cancel.cancelled:
            return CommandResult(display, None, Status.CANCELLED, error="cancelled before start", started_at=started_at)

        env = build_environment(spec.env, base=spec.env_base, inherit=spec.inherit_env)
        policy = spec.retry
        outcome: ProcessOutcome | None = None
        status = Status.FAILED
        attempt = 0
        for attempt in range(1, policy.attempts + 1):
            self.events.emit(COMMAND_STARTED, command=display, attempt=attempt, name=spec.name)
            outcome = self._runner(
                argv,
                cwd=spec.cwd,
                env=env,
                timeout=spec.timeout,
                cancel=cancel,
                on_line=on_line,
                stdin_data=spec.stdin_data,
                interactive=spec.interactive,
            )
            status = _status_for(outcome)
            if status in (Status.SUCCESS, Status.CANCELLED):
                break
            if status == Status.TIMEOUT and not policy.retry_on_timeout:
                break
            if outcome.exit_code in NON_RETRYABLE_EXIT_CODES and outcome.error:
                break
            if attempt < policy.attempts:
                delay = policy.delay_before(attempt)
                self.events.emit(
                    COMMAND_RETRY, command=display, attempt=attempt, delay=delay, exit_code=outcome.exit_code
                )
                if self._sleep(delay, cancel):
                    status = Status.CANCELLED
                    break

        assert outcome is not None
        error = outcome.error
        if status == Status.TIMEOUT:
            error = f"timed out after {spec.timeout:g}s" if spec.timeout else "timed out"
        elif status == Status.CANCELLED and not error:
            error = f"cancelled ({cancel.reason})" if cancel and cancel.reason else "cancelled"
        result = CommandResult(
            command=display,
            exit_code=outcome.exit_code,
            status=status,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration=time.monotonic() - began,
            attempts=attempt,
            started_at=started_at,
            error=error,
        )
        self.events.emit(
            COMMAND_FINISHED,
            command=display,
            status=str(status),
            exit_code=outcome.exit_code,
            duration=result.duration,
            name=spec.name,
        )
        return result
