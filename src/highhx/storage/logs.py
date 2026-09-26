"""Per-execution log files (redacted, append-only)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from highhx.security.secrets import Redactor
from highhx.utils.filesystem import ensure_dir
from highhx.utils.time import iso_now


class LogWriter:
    """Thread-safe appender for one execution's log file."""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.path = path
        self.redactor = redactor
        self._lock = threading.Lock()
        ensure_dir(path.parent)

    def write(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None:
        text = self.redactor.redact(line.rstrip("\n"))
        prefix = f"{iso_now()} {stream:<6}"
        if source:
            prefix += f" [{source}]"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix} {text}\n")

    def event(self, message: str) -> None:
        self.write(message, stream="highhx")


class LogStore:
    """Manages ``<logs_dir>/<execution-id>.log`` files."""

    def __init__(self, directory: Path, redactor: Redactor | None = None) -> None:
        self.directory = directory
        self.redactor = redactor or Redactor()

    def path_for(self, execution_id: str) -> Path:
        safe = "".join(ch for ch in execution_id if ch.isalnum() or ch in "-_T")
        return self.directory / f"{safe}.log"

    def writer(self, execution_id: str) -> LogWriter:
        return LogWriter(self.path_for(execution_id), self.redactor)

    def exists(self, execution_id: str) -> bool:
        return self.path_for(execution_id).exists()

    def read(self, execution_id: str, *, tail: int | None = None) -> list[str]:
        path = self.path_for(execution_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-tail:] if tail else lines

    def follow(
        self,
        execution_id: str,
        *,
        is_running: Callable[[], bool],
        poll: float = 0.25,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """Yield lines as they are appended until the execution finishes."""
        path = self.path_for(execution_id)
        position = 0
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    for line in handle:
                        yield line.rstrip("\n")
                    position = handle.tell()
            if stop is not None and stop():
                return
            if not is_running():
                # Drain anything written after the final check.
                if path.exists():
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(position)
                        for line in handle:
                            yield line.rstrip("\n")
                return
            time.sleep(poll)

    def list_ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted((p.stem for p in self.directory.glob("*.log")), reverse=True)

    def prune(self, keep_ids: set[str]) -> int:
        removed = 0
        if not self.directory.exists():
            return 0
        for path in self.directory.glob("*.log"):
            if path.stem not in keep_ids:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def ensure(self) -> Path:
        return ensure_dir(self.directory)
