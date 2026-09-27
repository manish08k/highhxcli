"""Running workflow executions, visible to every HighhX process (for `highhx workflow cancel`)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.utils.filesystem import atomic_write_text
from highhx.utils.paths import user_data_dir


@dataclass(frozen=True)
class RunningWorkflow:
    execution_id: str
    workflow: str
    pid: int
    root: str
    started: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _dir() -> Path:
    return user_data_dir() / "running-workflows"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def registered(execution_id: str, workflow: str, root: Path) -> Iterator[None]:
    path = _dir() / f"{execution_id}.json"
    entry = RunningWorkflow(execution_id, workflow, os.getpid(), str(root), time.time())
    try:
        atomic_write_text(path, json.dumps(entry.to_dict()), mode=0o600)
    except OSError:
        pass  # the registry only enables remote cancellation; never block a run on it
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def running() -> list[RunningWorkflow]:
    found: list[RunningWorkflow] = []
    for path in sorted(_dir().glob("*.json")) if _dir().is_dir() else []:
        try:
            entry = RunningWorkflow(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if _alive(entry.pid):
            found.append(entry)
        else:
            with contextlib.suppress(OSError):
                path.unlink()  # stale entry of a process that died
    return found


def find(execution_id: str) -> RunningWorkflow | None:
    matches = [r for r in running() if r.execution_id == execution_id or r.execution_id.startswith(execution_id)]
    return matches[0] if len(matches) == 1 else None


def cancel(entry: RunningWorkflow) -> bool:
    """Interrupt the process running ``entry``: it cancels the run, records it as cancelled and
    stops its commands (SIGINT on POSIX — the same as Ctrl+C there; SIGTERM on Windows)."""
    if entry.pid == os.getpid():
        return False
    sig = signal.SIGTERM if os.name == "nt" else signal.SIGINT
    try:
        os.kill(entry.pid, sig)
    except OSError:
        return False
    return True
