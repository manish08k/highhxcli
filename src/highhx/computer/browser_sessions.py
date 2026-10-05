"""Managed browser sessions: one interface for HighhX's own browsers and remote ones.

    start(profile | endpoint) → session id, browser id, DevTools endpoint (loopback for local)
    heartbeat(id)             → is the browser answering; the time of the last answer
    reconnect(id)             → a local browser that died is started again on the same profile;
                                a remote one is reconnected (HighhX never starts remote browsers)
    stop(id)                  → a local browser quits gracefully (profile saved); a remote one is
                                only disconnected
    cleanup()                 → forget sessions whose browser or owner is gone, or idle too long

Sessions are records in ``<computer state>/browser-sessions.json`` (owner-only). A local session
leases its profile, so two sessions never drive the same profile at once; several profiles can
run side by side. A remote endpoint's token (``?token=…``) is never stored or shown: the record
keeps the endpoint without its query, and the full endpoint stays in the process that started it
(or in ``HIGHHX_BROWSER_ENDPOINT``).

This module only manages lifecycles. Every action on a page still goes through the executor
with ``HIGHHX_BROWSER_PROFILE`` / ``ComputerSession(profile=…)`` choosing the browser.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from highhx.computer.profiles import DEFAULT, ProfileStore
from highhx.core.errors import IntegrationError, UsageError
from highhx.utils.hashing import new_id

IDLE_LIMIT = 6 * 3600.0
"""A session nobody has heartbeated for this long is cleaned up."""


@dataclass
class BrowserSession:
    id: str
    kind: str
    """``local`` (HighhX started it) or ``remote`` (an existing DevTools endpoint)."""
    profile: str
    browser_id: str
    """The browser process id (local) or the endpoint without its query (remote)."""
    devtools: str
    """``http://127.0.0.1:PORT`` (local) or the endpoint without its query (remote)."""
    owner_pid: int
    started: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    healthy: bool = True
    reconnects: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


BrowserFactory = Callable[[Path, str], Any]
"""``(state_dir, endpoint)`` → a ChromeBrowser ('' endpoint) or RemoteBrowser."""


def _default_factory(state_dir: Path, endpoint: str, headless: bool | None = None) -> Any:
    from highhx.computer.browser import ChromeBrowser, RemoteBrowser

    return RemoteBrowser(state_dir, endpoint) if endpoint else ChromeBrowser(state_dir, headless=headless)


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


class BrowserSessionManager:
    def __init__(self, base: Path, *, factory: BrowserFactory | None = None, headless: bool | None = None) -> None:
        self.base = base
        self.profiles = ProfileStore(base)
        self.file = base / "browser-sessions.json"
        self.headless = headless
        self.factory = factory or (lambda state_dir, endpoint: _default_factory(state_dir, endpoint, self.headless))
        self._remote_endpoints: dict[str, str] = {}
        """Full remote endpoints (with tokens) of sessions started by this process only."""

    # ---------------------------------------------------------------- records
    def _load(self) -> dict[str, BrowserSession]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for item in raw if isinstance(raw, list) else []:
            with contextlib.suppress(TypeError):
                session = BrowserSession(**item)
                out[session.id] = session
        return out

    def _save(self, sessions: dict[str, BrowserSession]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump([s.to_dict() for s in sessions.values()], out, indent=2)
        tmp.replace(self.file)

    def get(self, session_id: str) -> BrowserSession:
        found = self._load().get(session_id)
        if found is None:
            raise UsageError(f"No browser session {session_id!r}.", hint="See `highhx browser sessions`.")
        return found

    def list(self) -> builtins.list[BrowserSession]:
        return sorted(self._load().values(), key=lambda s: s.started)

    def _browser(self, session: BrowserSession) -> Any:
        endpoint = self._remote_endpoints.get(session.id, session.devtools) if session.kind == "remote" else ""
        return self.factory(self.profiles.state_dir(session.profile), endpoint)

    # -------------------------------------------------------------- lifecycle
    def start(self, *, profile: str = DEFAULT, endpoint: str = "", cancel: Any = None) -> BrowserSession:
        session_id = new_id("bs")
        if endpoint:
            browser = self.factory(self.profiles.state_dir(DEFAULT), endpoint)
            browser.start(cancel=cancel)
            shown = endpoint.split("?", 1)[0]
            session = BrowserSession(session_id, "remote", DEFAULT, shown, shown, os.getpid())
            self._remote_endpoints[session_id] = endpoint
        else:
            self.profiles.lease(profile, session_id)
            try:
                state = self.factory(self.profiles.state_dir(profile), "").start(cancel=cancel)
            except Exception:
                self.profiles.release(profile, session_id)
                raise
            # the lease lives as long as the browser (a CLI that started the session may exit)
            self.profiles.lease(profile, session_id, pid=int(state.get("pid") or 0) or None)
            self.profiles.touch(profile)
            session = BrowserSession(
                session_id,
                "local",
                profile,
                str(state.get("pid") or ""),
                f"http://127.0.0.1:{state.get('port')}",
                os.getpid(),
            )
        sessions = self._load()
        sessions[session_id] = session
        self._save(sessions)
        return session

    def heartbeat(self, session_id: str) -> BrowserSession:
        sessions = self._load()
        session = sessions.get(session_id)
        if session is None:
            raise UsageError(f"No browser session {session_id!r}.")
        browser = self._browser(session)
        session.healthy = browser._state() is not None
        if session.healthy:
            session.last_heartbeat = time.time()
        self._save(sessions)
        return session

    def reconnect(self, session_id: str, *, cancel: Any = None) -> BrowserSession:
        """Bring the session back: a local browser that died is started again on its profile."""
        sessions = self._load()
        session = sessions.get(session_id)
        if session is None:
            raise UsageError(f"No browser session {session_id!r}.")
        browser = self._browser(session)
        try:
            state = browser.start(cancel=cancel)  # local: restarts a dead browser on the same profile
        except IntegrationError:
            session.healthy = False
            self._save(sessions)
            raise
        if session.kind == "local":
            session.browser_id = str(state.get("pid") or "")
            session.devtools = f"http://127.0.0.1:{state.get('port')}"
            self.profiles.lease(session.profile, session.id, pid=int(state.get("pid") or 0) or None)
        session.healthy, session.last_heartbeat = True, time.time()
        session.reconnects += 1
        self._save(sessions)
        return session

    def stop(self, session_id: str) -> None:
        sessions = self._load()
        session = sessions.pop(session_id, None)
        if session is None:
            raise UsageError(f"No browser session {session_id!r}.")
        try:
            self._browser(session).stop()  # local: graceful quit; remote: disconnect only
        finally:
            if session.kind == "local":
                self.profiles.release(session.profile, session_id)
            self._remote_endpoints.pop(session_id, None)
            self._save(sessions)

    def cleanup(self, *, idle: float = IDLE_LIMIT) -> builtins.list[str]:
        """Forget sessions whose local browser died, remote sessions whose starting process exited
        (their full endpoint left with it), and sessions idle past ``idle`` seconds; their local
        browsers are stopped and profiles released. A local session outlives the CLI that started it."""
        removed = []
        for session in self.list():
            dead_browser = session.kind == "local" and not _alive(int(session.browser_id or 0))
            orphaned_remote = session.kind == "remote" and not _alive(session.owner_pid)
            idle_too_long = time.time() - session.last_heartbeat > idle
            if dead_browser or orphaned_remote or idle_too_long:
                with contextlib.suppress(Exception):
                    self.stop(session.id)
                removed.append(session.id)
        return removed

    def screenshot(self, session_id: str, *, cancel: Any = None) -> bytes:
        return bytes(self._browser(self.get(session_id)).screenshot(cancel=cancel))
