"""Subprocess management with streaming output, timeouts and cancellation."""

from __future__ import annotations

import contextlib
import locale
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from highhx.execution.cancellation import CancellationToken
from highhx.execution.timeout import Deadline
from highhx.utils.platform import IS_WINDOWS

LineCallback = Callable[[str, str], None]
"""Called with ``(stream, line)`` where stream is ``"stdout"`` or ``"stderr"``."""

MAX_CAPTURE_BYTES = 4 * 1024 * 1024
TERMINATE_GRACE = 5.0
POLL_INTERVAL = 0.05


@dataclass
class ProcessOutcome:
    """Raw outcome of a single process run."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    pid: int | None = None
    error: str | None = None


class _Capture:
    """Bounded line collector for one stream."""

    def __init__(self, limit: int) -> None:
        self.lines: list[str] = []
        self.size = 0
        self.limit = limit
        self.truncated = False

    def add(self, line: str) -> None:
        if self.size + len(line) > self.limit:
            if not self.truncated:
                self.truncated = True
                self.lines.append("[... output truncated by HighhX ...]\n")
            return
        self.lines.append(line)
        self.size += len(line)

    def text(self) -> str:
        return "".join(self.lines)


def _decode(raw: bytes) -> str:
    """UTF-8 first; fall back to the locale encoding (e.g. legacy Windows code pages)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def _reader(pipe, name: str, capture: _Capture, callback: LineCallback | None) -> None:  # type: ignore[no-untyped-def]
    try:
        for raw in iter(pipe.readline, b""):
            line = _decode(raw)
            if line.endswith("\r\n"):
                line = line[:-2] + "\n"
            capture.add(line)
            if callback is not None:
                with contextlib.suppress(Exception):
                    callback(name, line.rstrip("\n"))
    finally:
        with contextlib.suppress(Exception):
            pipe.close()


def _terminate(proc: subprocess.Popen[bytes], *, own_group: bool) -> None:
    """Gracefully stop ``proc`` and its children, escalating to a hard kill."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], capture_output=True, check=False)
        try:
            proc.wait(TERMINATE_GRACE)
            return
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(2.0)
            return

    def _signal(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if own_group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)

    _signal(signal.SIGTERM)
    try:
        proc.wait(TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        _signal(signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(2.0)


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cancel: CancellationToken | None = None,
    on_line: LineCallback | None = None,
    stdin_data: str | None = None,
    interactive: bool = False,
    max_capture: int = MAX_CAPTURE_BYTES,
) -> ProcessOutcome:
    """Run ``argv`` to completion.

    Non-interactive processes run in their own process group/session so that a
    timeout or cancellation can terminate the whole tree. Interactive processes
    inherit the terminal (stdin/stdout/stderr) and are not captured.
    """
    started = time.monotonic()
    deadline = Deadline.after(timeout)
    popen_kwargs: dict[str, object] = {"cwd": str(cwd) if cwd else None, "env": dict(env) if env is not None else None}
    own_group = False
    if not interactive:
        popen_kwargs.update(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True
            own_group = True
    try:
        proc = subprocess.Popen(list(argv), **popen_kwargs)  # type: ignore[call-overload]
    except FileNotFoundError:
        return ProcessOutcome(127, "", "", time.monotonic() - started, error=f"command not found: {argv[0]}")
    except PermissionError:
        return ProcessOutcome(126, "", "", time.monotonic() - started, error=f"permission denied: {argv[0]}")
    except NotADirectoryError:
        return ProcessOutcome(126, "", "", time.monotonic() - started, error=f"invalid working directory: {cwd}")
    except OSError as exc:
        return ProcessOutcome(126, "", "", time.monotonic() - started, error=f"cannot start process: {exc}")

    out_cap, err_cap = _Capture(max_capture), _Capture(max_capture)
    threads: list[threading.Thread] = []
    if not interactive:
        if stdin_data is not None and proc.stdin is not None:

            def _feed() -> None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    assert proc.stdin is not None
                    proc.stdin.write(stdin_data.encode("utf-8"))
                    proc.stdin.close()

            threads.append(threading.Thread(target=_feed, daemon=True))
        threads.append(threading.Thread(target=_reader, args=(proc.stdout, "stdout", out_cap, on_line), daemon=True))
        threads.append(threading.Thread(target=_reader, args=(proc.stderr, "stderr", err_cap, on_line), daemon=True))
        for thread in threads:
            thread.start()

    timed_out = cancelled = False
    try:
        while proc.poll() is None:
            if deadline.expired:
                timed_out = True
                _terminate(proc, own_group=own_group)
                break
            if cancel is not None and cancel.cancelled:
                cancelled = True
                _terminate(proc, own_group=own_group)
                break
            if cancel is not None:
                cancel.wait(POLL_INTERVAL)
            else:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        cancelled = True
        _terminate(proc, own_group=own_group)

    for thread in threads:
        thread.join(timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1.0)

    exit_code = proc.returncode
    return ProcessOutcome(
        exit_code=exit_code,
        stdout=out_cap.text(),
        stderr=err_cap.text(),
        duration=time.monotonic() - started,
        timed_out=timed_out,
        cancelled=cancelled,
        pid=proc.pid,
    )
