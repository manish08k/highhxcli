"""Registry of running agent processes — the kill switch behind `highhx agent stop`.

Each `highhx agent` process registers ``<user data>/agent/running/<pid>.json`` and
removes it on exit. Stopping sends SIGTERM, which the agent turns into
cancellation of the running turn: the model stream is closed (and cancelled on
the platform), retries stop, and running commands' process trees are terminated.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess  # nosec B404 - subprocess used with fixed argv only
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from highhx.utils.filesystem import atomic_write_text
from highhx.utils.paths import user_data_dir


@dataclass
class RunningAgent:
    pid: int
    session_id: str | None
    root: str
    started: float

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _dir() -> Path:
    return user_data_dir() / "agent" / "running"


def _alive(pid: int) -> bool:
    """True while ``pid`` is a running process. A zombie (exited, not yet reaped by its parent)
    counts as stopped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return not _zombie(pid)


def _zombie(pid: int) -> bool:
    if sys.platform == "win32":
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False
    try:
        state = subprocess.run(  # nosec B603 B607 - fixed argv, no shell; well-known system tool resolved from PATH
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return state.stdout.strip().startswith("Z")


@contextlib.contextmanager
def registered(session_id: str | None, root: Path) -> Iterator[None]:
    path = _dir() / f"{os.getpid()}.json"
    entry = RunningAgent(os.getpid(), session_id, str(root), time.time())
    atomic_write_text(path, json.dumps(entry.to_dict()), mode=0o600)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def running() -> list[RunningAgent]:
    found = []
    directory = _dir()
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text())
            agent = RunningAgent(
                int(data["pid"]), data.get("session_id"), str(data.get("root") or ""), float(data.get("started") or 0)
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if agent.pid == os.getpid():
            continue
        if not _alive(agent.pid):
            with contextlib.suppress(OSError):
                path.unlink()  # left behind by a crashed process
            continue
        found.append(agent)
    return found


def stop(agent: RunningAgent, *, timeout: float = 10.0) -> bool:
    """Signal ``agent`` to stop; True once the process is gone."""
    sig = signal.SIGTERM if sys.platform != "win32" else signal.SIGINT
    try:
        os.kill(agent.pid, sig)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(agent.pid, os.WNOHANG)
        if not _alive(agent.pid):
            return True
        time.sleep(0.05)
    return False
