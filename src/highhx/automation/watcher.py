"""Portable polling file watcher (no native dependencies)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from highhx.execution.cancellation import CancellationToken
from highhx.utils.filesystem import DEFAULT_IGNORE_DIRS, iter_files, matches_any

Snapshot = dict[str, tuple[int, int]]


@dataclass
class ChangeSet:
    """Files that changed between two snapshots (relative, forward-slash paths)."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    @property
    def all(self) -> list[str]:
        return sorted({*self.added, *self.modified, *self.removed})

    def merge(self, other: ChangeSet) -> None:
        self.added = sorted({*self.added, *other.added})
        self.modified = sorted({*self.modified, *other.modified})
        self.removed = sorted({*self.removed, *other.removed})


def diff_snapshots(old: Snapshot, new: Snapshot) -> ChangeSet:
    return ChangeSet(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
        modified=sorted(k for k in set(old) & set(new) if old[k] != new[k]),
    )


class FileWatcher:
    """Detects file changes by periodically comparing (mtime, size) snapshots."""

    def __init__(
        self,
        root: Path,
        *,
        paths: Iterable[str] = (".",),
        patterns: Iterable[str] = (),
        ignore: Iterable[str] = (),
        interval: float = 0.5,
        ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
    ) -> None:
        self.root = root.resolve()
        self.paths = list(paths) or ["."]
        self.patterns = list(patterns)
        self.ignore = list(ignore)
        self.interval = interval
        self.ignore_dirs = set(ignore_dirs)

    def _wanted(self, rel: str) -> bool:
        if self.ignore and matches_any(rel, self.ignore):
            return False
        return not self.patterns or matches_any(rel, self.patterns)

    def snapshot(self) -> Snapshot:
        snap: Snapshot = {}
        for entry in self.paths:
            base = (self.root / entry).resolve()
            if base.is_file():
                candidates: Iterable[Path] = [base]
            elif base.is_dir():
                candidates = iter_files(base, ignore_dirs=self.ignore_dirs)
            else:
                continue
            for path in candidates:
                try:
                    rel = path.relative_to(self.root).as_posix()
                except ValueError:
                    rel = path.as_posix()
                if not self._wanted(rel):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snap[rel] = (stat.st_mtime_ns, stat.st_size)
        return snap

    def watch(
        self,
        callback: Callable[[ChangeSet], None],
        *,
        cancel: CancellationToken,
        debounce: float = 0.3,
        max_events: int | None = None,
    ) -> int:
        """Invoke ``callback`` for each debounced batch of changes until cancelled.

        Returns the number of batches delivered.
        """
        previous = self.snapshot()
        delivered = 0
        while not cancel.cancelled:
            if cancel.wait(self.interval):
                break
            current = self.snapshot()
            changes = diff_snapshots(previous, current)
            if not changes:
                continue
            # Debounce: keep collecting while files are still changing.
            settle_until = time.monotonic() + debounce
            while time.monotonic() < settle_until and not cancel.cancelled:
                cancel.wait(min(0.1, debounce))
                newer = self.snapshot()
                extra = diff_snapshots(current, newer)
                if extra:
                    changes.merge(extra)
                    current = newer
                    settle_until = time.monotonic() + debounce
            previous = current
            callback(changes)
            delivered += 1
            if max_events is not None and delivered >= max_events:
                break
        return delivered
