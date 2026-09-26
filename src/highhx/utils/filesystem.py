"""Filesystem helpers used throughout HighhX."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

DEFAULT_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".next",
        ".nuxt",
        ".dart_tool",
        ".gradle",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        "coverage",
        ".highhx",
    }
)


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, *, mode: int | None = None, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (write temp file, then replace).

    ``mode`` sets POSIX permissions on the new file (ignored where unsupported).
    """
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        if mode is not None:
            with contextlib.suppress(OSError):
                tmp.chmod(mode)
        elif path.exists():
            with contextlib.suppress(OSError):
                tmp.chmod(stat.S_IMODE(path.stat().st_mode))
        else:
            with contextlib.suppress(OSError):
                tmp.chmod(0o666 & ~_current_umask())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def read_text(path: Path, *, limit: int | None = None) -> str:
    """Read a text file leniently (invalid bytes replaced). ``limit`` caps bytes read."""
    with path.open("rb") as handle:
        data = handle.read(limit) if limit else handle.read()
    return data.decode("utf-8", errors="replace")


def is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    """Heuristically detect binary files by looking for NUL bytes."""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(sample_size)
    except OSError:
        return True
    return b"\x00" in chunk


def matches_any(relpath: str, patterns: Iterable[str]) -> bool:
    """Return True if a forward-slash relative path matches any glob pattern.

    Patterns match either the full path or its final component.
    """
    name = relpath.rsplit("/", 1)[-1]
    for pattern in patterns:
        clean = pattern.rstrip("/")
        if fnmatch.fnmatch(relpath, clean) or fnmatch.fnmatch(name, clean):
            return True
        if relpath.startswith(clean + "/"):
            return True
    return False


def iter_files(
    root: Path,
    *,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
    ignore_patterns: Iterable[str] = (),
    max_size: int | None = None,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Yield files below ``root`` deterministically (sorted), skipping ignored directories."""
    ignored = set(ignore_dirs)
    patterns = tuple(ignore_patterns)
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in ignored and not matches_any(f"{rel_dir}/{d}".lstrip("./") if rel_dir != "." else d, patterns)
        )
        for filename in sorted(filenames):
            path = current / filename
            rel = path.relative_to(root).as_posix()
            if patterns and matches_any(rel, patterns):
                continue
            if not follow_symlinks and path.is_symlink():
                continue
            if max_size is not None:
                try:
                    if path.stat().st_size > max_size:
                        continue
                except OSError:
                    continue
            yield path


def path_size(path: Path) -> int:
    """Total size in bytes of a file or directory tree (symlinks not followed)."""
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            file_path = Path(dirpath) / name
            with contextlib.suppress(OSError):
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
    return total


def _on_rm_error(func, path, _exc):  # type: ignore[no-untyped-def]
    """Retry removal after clearing the read-only bit (needed on Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_path(path: Path) -> None:
    """Remove a file, symlink or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_on_rm_error)
        else:  # pragma: no cover - Python 3.11
            shutil.rmtree(path, onerror=_on_rm_error)


def human_size(num: float) -> str:
    """Format a byte count as a human readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def ensure_gitignore_entries(gitignore: Path, entries: Iterable[str]) -> list[str]:
    """Append missing ``entries`` to a .gitignore file. Returns the entries added."""
    existing: set[str] = set()
    content = ""
    if gitignore.exists():
        content = read_text(gitignore)
        existing = {line.strip() for line in content.splitlines()}
    missing = [entry for entry in entries if entry not in existing]
    if missing:
        prefix = "" if not content or content.endswith("\n") else "\n"
        block = prefix + "\n".join(["# HighhX local state", *missing]) + "\n"
        atomic_write_text(gitignore, content + block)
    return missing
