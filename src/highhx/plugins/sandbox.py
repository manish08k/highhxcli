"""Plugin trust model.

Python code cannot be sandboxed reliably inside the same interpreter, so HighhX
does not pretend to. Instead code plugins run only when *all* of these hold:

1. ``plugins.allow_code: true`` is set in the project config;
2. the exact plugin contents (SHA-256 of the directory) were trusted by *this user*
   through ``highhx plugin install`` or ``highhx plugin trust``. Trust is recorded in
   the user data directory, never inside the project, so a cloned repository cannot
   vouch for its own plugin code (its lock file and config are attacker-controlled).

Declarative plugin commands run as subprocesses with an isolated environment
(only safe variables) unless the plugin declares the ``env`` permission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.execution.isolation import isolated_environment
from highhx.plugins.manifest import PluginManifest
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.hashing import sha256_tree
from highhx.utils.time import iso_now


@dataclass
class LockEntry:
    name: str
    version: str
    source: str
    sha256: str
    installed_at: str
    code: bool
    permissions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def read_lock(path: Path) -> dict[str, LockEntry]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {name: LockEntry(**entry) for name, entry in data.get("plugins", {}).items()}


def write_lock(path: Path, entries: dict[str, LockEntry]) -> None:
    payload = {"version": 1, "plugins": {name: e.to_dict() for name, e in sorted(entries.items())}}
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


class TrustStore:
    """User-level record of plugin contents (by SHA-256) the user has approved."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return plugins if isinstance(plugins, dict) else {}

    def is_trusted(self, digest: str) -> bool:
        return digest in self._read()

    def trust(self, digest: str, manifest: PluginManifest, source: str) -> None:
        entries = self._read()
        entries[digest] = {
            "name": manifest.name,
            "version": manifest.version,
            "source": source,
            "trusted_at": iso_now(),
        }
        atomic_write_text(self.path, json.dumps({"version": 1, "plugins": entries}, indent=2) + "\n", mode=0o600)


def integrity_problem(manifest: PluginManifest, trust: TrustStore | None) -> str | None:
    """Why code of ``manifest`` must not run, or None if it may."""
    symlinks = symlinked_files(manifest.directory)
    if symlinks:
        return f"contains symbolic links ({', '.join(symlinks[:3])})"
    if trust is None or not trust.is_trusted(sha256_tree(manifest.directory)):
        return (
            "these exact files were not trusted by you — review them, then run `highhx plugin trust "
            + manifest.name
            + "`"
        )
    return None


def symlinked_files(directory: Path) -> list[str]:
    """Relative paths of symlinks inside a plugin (never allowed: they could point anywhere)."""
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_symlink())


def command_environment(manifest: PluginManifest, extra: dict[str, str] | None = None) -> dict[str, str] | None:
    """Environment for declarative plugin commands (None means inherit everything)."""
    if "env" in manifest.permissions:
        return None
    return isolated_environment(
        extra={"HIGHHX_PLUGIN": manifest.name, "HIGHHX_PLUGIN_DIR": str(manifest.directory), **(extra or {})}
    )
