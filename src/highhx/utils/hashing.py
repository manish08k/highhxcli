"""Hashing and identifier helpers."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Hex SHA-256 digest of UTF-8 ``text``."""
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    """Hex SHA-256 digest of a file's contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Deterministic digest of a directory tree (relative paths + contents)."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in {"__pycache__", ".git"})
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def new_id(prefix: str = "") -> str:
    """Return a sortable, unique identifier such as ``20260926T101500-3f9a1c2b4d6e``. The random
    part is 48 bits: many executions can start within one second (an agent loop observes and
    acts several times a second) without colliding."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    token = secrets.token_hex(6)
    return f"{prefix}{stamp}-{token}"


def short_hash(value: str, length: int = 12) -> str:
    """Short stable hash of a string, useful for fingerprints."""
    return sha256_text(value)[:length]
