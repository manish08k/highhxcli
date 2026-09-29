"""The project's files as entities: name, kind, size and times — metadata only, never contents.

    index = ProjectFileIndex(root)
    index.find(FileQuery(kinds=("pdf",), modified_after=yesterday, sort="newest"))

Built lazily on the first query (never at startup), bounded (``MAX_FILES``), and rebuilt when
it is older than ``TTL`` seconds or on :meth:`refresh`. It sees exactly what HighhX's file
actions may see: files inside the project, never secret files (``.env``, keys …), never
dependency/build folders or HighhX's own state. Nothing outside the project root is indexed —
that is the confinement policy of every file action (:func:`highhx.agent.permissions.confine_path`).
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_FILES = 20_000
"""Stop indexing after this many files (a huge tree is reported as partial, never scanned forever)."""
TTL = 30.0

KINDS: dict[str, frozenset[str]] = {
    "pdf": frozenset({".pdf"}),
    "document": frozenset({".pdf", ".doc", ".docx", ".odt", ".rtf", ".md", ".rst", ".txt", ".pages", ".tex"}),
    "spreadsheet": frozenset({".xls", ".xlsx", ".ods", ".csv", ".numbers"}),
    "presentation": frozenset({".ppt", ".pptx", ".odp", ".key"}),
    "image": frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".heic", ".ico"}),
    "video": frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"}),
    "audio": frozenset({".mp3", ".wav", ".flac", ".m4a", ".ogg"}),
    "archive": frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}),
}
"""A file's kind from its extension (a PDF is also a document)."""


def kinds_of(path: str) -> tuple[str, ...]:
    suffix = Path(path).suffix.lower()
    return tuple(kind for kind, suffixes in KINDS.items() if suffix in suffixes)


@dataclass(frozen=True)
class FileEntity:
    path: str
    """Relative to the project root, with forward slashes."""
    size: int
    modified: float
    """Seconds since the epoch."""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def parent(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else "."

    @property
    def kinds(self) -> tuple[str, ...]:
        return kinds_of(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": (self.kinds or ("file",))[0],
            "size": self.size,
            "modified": datetime.fromtimestamp(self.modified).isoformat(timespec="seconds"),
        }


@dataclass(frozen=True)
class FileQuery:
    kinds: tuple[str, ...] = ()
    """Any of these kinds (``KINDS`` keys); empty: any file."""
    keywords: tuple[str, ...] = ()
    """Every keyword appears in the file's path (any case; ``-``, ``_`` and spaces are equal)."""
    modified_after: float | None = None
    modified_before: float | None = None
    sort: str = "newest"
    """newest, oldest or name."""
    under: str = "."
    """Only files inside this project folder."""
    limit: int = 50


def _words(text: str) -> str:
    return re.sub(r"[\s_\-.]+", " ", text.lower())


def matches(entity: FileEntity, query: FileQuery) -> bool:
    if query.kinds and not set(entity.kinds) & set(query.kinds):
        return False
    if query.under not in ("", ".") and not (entity.path + "/").startswith(query.under.rstrip("/") + "/"):
        return False
    if query.modified_after is not None and entity.modified < query.modified_after:
        return False
    if query.modified_before is not None and entity.modified >= query.modified_before:
        return False
    haystack = _words(entity.path)
    return all(_words(k).strip() in haystack for k in query.keywords)


def order(found: Iterable[FileEntity], sort: str) -> list[FileEntity]:
    if sort == "name":
        return sorted(found, key=lambda f: f.path.lower())
    newest = sort != "oldest"
    # ties broken by path so the same files always come back in the same order
    return sorted(found, key=lambda f: (-f.modified if newest else f.modified, f.path))


@dataclass
class ProjectFileIndex:
    root: Path
    ttl: float = TTL
    _files: list[FileEntity] | None = field(default=None, repr=False)
    _built: float = field(default=0.0, repr=False)
    partial: bool = False
    """The project has more than ``MAX_FILES`` files: only the first were indexed."""

    def files(self) -> list[FileEntity]:
        if self._files is None or time.monotonic() - self._built > self.ttl:
            self.refresh()
        assert self._files is not None
        return self._files

    def refresh(self) -> None:
        self._files, self.partial = _scan(self.root)
        self._built = time.monotonic()

    def find(self, query: FileQuery) -> list[FileEntity]:
        found = [f for f in self.files() if matches(f, query)]
        return order(found, query.sort)[: query.limit]


def _scan(root: Path) -> tuple[list[FileEntity], bool]:
    from highhx.actions.handlers.files import SKIP_DIRS
    from highhx.agent.permissions import HIDDEN_DIRS, is_secret_path

    base = root.resolve()
    out: list[FileEntity] = []
    for current, dirs, names in os.walk(base):
        here = Path(current)
        rel_dir = here.relative_to(base).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not any((rel_dir + d) == h or (rel_dir + d).startswith(h + "/") for h in HIDDEN_DIRS)
            and not (here / d).is_symlink()  # never follow a link out of the project
        )
        for name in sorted(names):
            rel = rel_dir + name
            if is_secret_path(rel):
                continue
            try:
                info = (here / name).stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):  # links and special files are never entities
                continue
            out.append(FileEntity(rel, info.st_size, info.st_mtime))
            if len(out) >= MAX_FILES:
                return out, True
    return out, False
