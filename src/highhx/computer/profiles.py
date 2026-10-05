"""Named profiles for the HighhX browser: isolated, persistent browser state (cookies, local
storage, preferences, signed-in sessions) that HighhX itself never reads.

    <computer state>/browser-profile          the "default" profile (unchanged location)
    <computer state>/profiles/<name>/         every other profile: its own browser-profile/,
                                              browser.json (its running browser) and profile.json

Each profile is a separate Chrome user-data directory and a separate browser process, so two
profiles never share cookies and can run at the same time. HighhX treats a profile's contents as
opaque: listing shows its size and whether its browser is running, never what is inside; nothing
from a profile reaches trajectories, prompts, logs, benchmarks or audit rows. Directories are
owner-only (0700).

Leases (:meth:`ProfileStore.lease`) give one task or session exclusive use of a profile; a lease
held by a process that has exited is stale and is broken.

Import copies a directory you name into a new profile (for example a profile you exported from
another HighhX machine) — the person's consent is the action's approval (high risk: it brings
signed-in sessions). Export is deliberately not offered: it would put credentials in a file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from highhx.core.errors import UsageError

DEFAULT = "default"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SKIPPED_ON_IMPORT = shutil.ignore_patterns(
    "Singleton*", "DevToolsActivePort", "*.lock", "Cache", "Code Cache", "GPUCache", "ShaderCache", "Crashpad"
)


def check_name(name: str) -> str:
    if not _NAME.match(name):
        raise UsageError(
            f"Invalid profile name {name!r}.", hint="Lower-case letters, digits, '-' and '_' (at most 40)."
        )
    return name


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


@dataclass(frozen=True)
class ProfileInfo:
    name: str
    path: str
    created: float
    last_used: float
    bytes: int
    running: bool
    """Its browser is running (HighhX's browser.json names a live process)."""
    leased_by: str
    """The session holding it exclusively ('' when free)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProfileStore:
    def __init__(self, base: Path) -> None:
        self.base = base
        """The computer state directory (``ComputerSession.state_dir`` of the default profile)."""

    # ------------------------------------------------------------------ paths
    def state_dir(self, name: str) -> Path:
        """The browser state directory of profile ``name`` (what ChromeBrowser takes)."""
        check_name(name)
        return self.base if name == DEFAULT else self.base / "profiles" / name

    def exists(self, name: str) -> bool:
        return name == DEFAULT or (self.state_dir(name) / "profile.json").is_file()

    def _meta(self, name: str) -> dict[str, Any]:
        path = self.state_dir(name) / "profile.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _write_meta(self, name: str, data: dict[str, Any]) -> None:
        path = self.state_dir(name) / "profile.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def running(self, name: str) -> bool:
        try:
            saved = json.loads((self.state_dir(name) / "browser.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return _alive(int(saved.get("pid") or 0)) if isinstance(saved, dict) else False

    # ------------------------------------------------------------- lifecycle
    def list(self) -> list[ProfileInfo]:
        names = [DEFAULT]
        root = self.base / "profiles"
        if root.is_dir():
            names += sorted(
                p.name for p in root.iterdir() if p.is_dir() and _NAME.match(p.name) and (p / "profile.json").is_file()
            )
        return [self.info(n) for n in names]

    def info(self, name: str) -> ProfileInfo:
        if not self.exists(name):
            raise UsageError(f"No browser profile named {name!r}.", hint="See `highhx browser profiles`.")
        meta = self._meta(name)
        data_dir = self.state_dir(name) / "browser-profile"
        return ProfileInfo(
            name,
            str(self.state_dir(name)),
            float(meta.get("created") or 0.0),
            float(meta.get("last_used") or 0.0),
            _size(data_dir) if data_dir.is_dir() else 0,
            self.running(name),
            self._lease_holder(name),
        )

    def create(self, name: str) -> ProfileInfo:
        check_name(name)
        if name == DEFAULT or self.exists(name):
            raise UsageError(f"The profile {name!r} already exists.")
        target = self.state_dir(name)
        (target / "browser-profile").mkdir(parents=True, mode=0o700)
        target.chmod(0o700)
        self._write_meta(name, {"name": name, "created": time.time(), "last_used": 0.0})
        return self.info(name)

    def touch(self, name: str) -> None:
        if name != DEFAULT and self.exists(name):
            meta = self._meta(name)
            meta["last_used"] = time.time()
            self._write_meta(name, meta)

    def delete(self, name: str) -> None:
        check_name(name)
        if name == DEFAULT:
            raise UsageError(
                "The default profile cannot be deleted (stop the browser and clear it with `highhx computer browser stop`)."
            )
        if not self.exists(name):
            raise UsageError(f"No browser profile named {name!r}.")
        if self.running(name):
            raise UsageError(f"The browser of profile {name!r} is running.", hint="Stop it first.")
        if self._lease_holder(name):
            raise UsageError(f"Profile {name!r} is in use by {self._lease_holder(name)}.")
        shutil.rmtree(self.state_dir(name))

    def import_from(self, name: str, source: Path) -> ProfileInfo:
        """A new profile holding a copy of ``source`` (a Chrome user-data directory). Locks and caches
        are left behind; the contents are never read, only copied."""
        check_name(name)
        if name == DEFAULT or self.exists(name):
            raise UsageError(f"The profile {name!r} already exists.")
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise UsageError(f"{source} is not a directory.")
        if (source / "SingletonLock").exists() or (source / "SingletonLock").is_symlink():
            raise UsageError(f"{source} is in use by a running browser; close it first.")
        target = self.state_dir(name)
        target.mkdir(parents=True, mode=0o700)
        shutil.copytree(source, target / "browser-profile", ignore=SKIPPED_ON_IMPORT, symlinks=True)
        (target / "browser-profile").chmod(0o700)
        self._write_meta(name, {"name": name, "created": time.time(), "last_used": 0.0, "imported": True})
        return self.info(name)

    # ----------------------------------------------------------------- leases
    def _lease_file(self, name: str) -> Path:
        return self.state_dir(name) / "profile.lease"

    def _lease_holder(self, name: str) -> str:
        try:
            data = json.loads(self._lease_file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict) or not _alive(int(data.get("pid") or 0)):
            return ""  # stale: its process has exited
        return str(data.get("owner") or "")

    def lease(self, name: str, owner: str, *, pid: int | None = None) -> None:
        """Exclusive use of ``name`` for ``owner`` (a session id) while process ``pid`` lives (default:
        this process; a browser session passes its browser's pid). Refused while another live owner holds it."""
        if not self.exists(name):
            raise UsageError(f"No browser profile named {name!r}.")
        holder = self._lease_holder(name)
        if holder and holder != owner:
            raise UsageError(
                f"Profile {name!r} is in use by {holder}.", hint="Use another profile, or stop that session."
            )
        path = self._lease_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"owner": owner, "pid": pid or os.getpid(), "since": time.time()}, out)

    def release(self, name: str, owner: str) -> None:
        if self._lease_holder(name) in (owner, ""):
            self._lease_file(name).unlink(missing_ok=True)
