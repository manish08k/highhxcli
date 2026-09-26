"""Cross-platform process helpers."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time

from highhx.utils.platform import IS_WINDOWS


def which(command: str, path: str | None = None) -> str | None:
    """Locate an executable on PATH (thin wrapper so tests can patch one place)."""
    return shutil.which(command, path=path)


def pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(pid: int, *, grace: float = 5.0, group: bool = True) -> bool:
    """Terminate ``pid`` (and its process group / children), escalating to kill.

    Returns True when the process is gone afterwards.
    """
    if not pid_alive(pid):
        return True
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
        if _wait_gone(pid, grace):
            return True
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return _wait_gone(pid, 2.0)

    def _send(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if group:
                try:
                    os.killpg(os.getpgid(pid), sig)
                    return
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            os.kill(pid, sig)

    _send(signal.SIGTERM)
    if _wait_gone(pid, grace):
        return True
    _send(signal.SIGKILL)
    return _wait_gone(pid, 2.0)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        _reap(pid)
        time.sleep(0.05)
    return not pid_alive(pid)


def _reap(pid: int) -> None:
    """Reap a zombie child so ``pid_alive`` reports correctly on POSIX."""
    if IS_WINDOWS:
        return
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)
