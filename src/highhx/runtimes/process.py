"""Running one command with a deadline, resource limits, cancellation and process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from highhx.runtimes.base import ExecResult, ResourceLimits

if TYPE_CHECKING:
    from highhx.execution.cancellation import CancellationToken

MAX_OUTPUT = 200_000


def limiter(limits: ResourceLimits) -> Callable[[], None] | None:
    """A ``preexec_fn`` applying ``limits`` with setrlimit (POSIX)."""
    if os.name != "posix":
        return None

    def apply() -> None:
        import resource

        def cap(kind: int, value: int) -> None:
            _soft, hard = resource.getrlimit(kind)
            limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
            try:
                resource.setrlimit(kind, (limit, limit if hard == resource.RLIM_INFINITY else hard))
            except (ValueError, OSError):
                pass

        if limits.cpu_seconds:
            cap(resource.RLIMIT_CPU, int(limits.cpu_seconds))
        if limits.file_mb:
            cap(resource.RLIMIT_FSIZE, int(limits.file_mb) * 1024 * 1024)
        if limits.memory_mb and sys.platform.startswith("linux"):
            cap(resource.RLIMIT_AS, int(limits.memory_mb) * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def kill_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    limits: ResourceLimits | None = None,
    cancel: CancellationToken | None = None,
    on_start: Callable[[int], None] | None = None,
) -> ExecResult:
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
        preexec_fn=limiter(limits) if limits is not None else None,  # noqa: PLW1509 - setrlimit only
    )
    if on_start is not None:
        on_start(process.pid)
    timed_out = cancelled = False
    with process:
        while True:
            try:
                out, err = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.cancelled:
                    cancelled = True
                elif time.monotonic() - started > timeout:
                    timed_out = True
                else:
                    continue
                kill_group(process)
                out, err = process.communicate()
                break
    if os.name == "posix":  # background children of the command never outlive it
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    truncated = len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT
    return ExecResult(
        process.returncode if process.returncode is not None else -1,
        stdout[-MAX_OUTPUT:],
        stderr[-MAX_OUTPUT:],
        time.monotonic() - started,
        timed_out,
        cancelled,
        truncated,
    )
