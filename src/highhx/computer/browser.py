"""Chrome / Chromium / Edge / Brave automation through the Chrome DevTools Protocol.

The browser is started with a dedicated HighhX profile (never the user's
personal profile) and a DevTools port bound to 127.0.0.1. Observations come from
the DOM with accessible names; password and payment field values never leave
the page. The session is recorded in the user state directory so separate
`highhx computer` invocations reuse the same browser — and the same tab.

Architecture (one place for each concern):

* :mod:`~highhx.computer.cdp` — the transport: one WebSocket to the browser, a flat session
  per tab, and errors that say whether a command can have run.
* :mod:`~highhx.computer.tabs` — the tab registry, kept current from target events.
* :class:`ChromeBrowser` — the lifecycle (start, reconnect, restart a crashed or hung
  browser), tab selection, and :meth:`ChromeBrowser._run`, the single recovery policy every
  action goes through (:class:`Retry`: safe actions are recovered and repeated, unsafe ones
  are recovered and reported, never repeated). Every transition is journaled as a
  :class:`BrowserState` change and ends up in the audit trail.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess  # nosec B404 - subprocess used with fixed argv only
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from highhx.computer.cdp import (
    BrowserDisconnectedError,
    BrowserTimeoutError,
    CDPConnection,
    CDPError,
    PageCrashedError,
    TargetClosedError,
)
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.computer.tabs import TabRegistry, same_document
from highhx.computer.websocket import WebSocketClosed
from highhx.core.errors import (
    IntegrationError,
    NotFoundError,
    OperationCancelledError,
    OutcomeUnknownError,
    ToolNotFoundError,
    UsageError,
)
from highhx.execution.cancellation import CancellationToken
from highhx.utils.filesystem import atomic_write_text

MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)
WINDOWS_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

OBSERVE_JS = r"""
(() => {
  const selector = 'a[href],button,input,select,textarea,summary,[role],[contenteditable="true"],[onclick],h1,h2,h3';
  document.querySelectorAll('[data-highhx-id]').forEach(e => e.removeAttribute('data-highhx-id'));
  const secretInput = e => {
    const t = (e.getAttribute('type') || '').toLowerCase();
    const a = (e.getAttribute('autocomplete') || '').toLowerCase();
    return t === 'password' || a.includes('password') || a.startsWith('cc-') || a.includes('one-time-code');
  };
  const visible = e => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const roleOf = e => {
    const explicit = (e.getAttribute('role') || '').toLowerCase().split(' ')[0];
    if (explicit) return explicit === 'searchbox' ? 'searchbox' : explicit;
    const tag = e.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (/^h[1-3]$/.test(tag)) return 'heading';
    if (e.isContentEditable) return 'textbox';
    if (tag === 'input') {
      const t = (e.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'search') return 'searchbox';
      if (t === 'range') return 'slider';
      if (t === 'hidden') return '';
      return 'textbox';
    }
    return e.hasAttribute('onclick') ? 'button' : '';
  };
  const text = s => (s || '').replace(/\s+/g, ' ').trim();
  const inner = e => (e ? (e.innerText ?? e.textContent ?? '') : '');
  const nameOf = e => {
    if (e.getAttribute('aria-label')) return text(e.getAttribute('aria-label'));
    const by = e.getAttribute('aria-labelledby');
    if (by) return text(by.split(' ').map(i => inner(document.getElementById(i))).join(' '));
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return text(inner(l)); }
    const wrap = e.closest('label'); if (wrap && wrap !== e) return text(inner(wrap));
    const tag = e.tagName.toLowerCase();
    if (tag === 'input' && ['button', 'submit', 'reset'].includes((e.type || '').toLowerCase())) return text(e.value);
    if (tag === 'img' || e.getAttribute('alt')) return text(e.getAttribute('alt'));
    if (['a', 'button', 'summary', 'h1', 'h2', 'h3'].includes(tag) || e.getAttribute('role')) {
      const t = text(inner(e)); if (t) return t;
    }
    return text(e.getAttribute('title') || e.getAttribute('placeholder') || e.getAttribute('name') || '');
  };
  const out = [];
  let n = 0;
  for (const e of document.querySelectorAll(selector)) {
    if (!visible(e)) continue;
    const role = roleOf(e);
    if (!role) continue;
    const id = 'e' + (++n);
    e.setAttribute('data-highhx-id', id);
    const form = e.form || e.closest('form');
    const secret = e.tagName === 'INPUT' && secretInput(e);
    const r = e.getBoundingClientRect();
    const checkable = e.type === 'checkbox' || e.type === 'radio';
    out.push({
      id, role, name: nameOf(e).slice(0, 160),
      value: secret ? '' : String(e.value ?? (e.isContentEditable ? inner(e) : '') ?? '').slice(0, 300),
      enabled: !e.disabled && e.getAttribute('aria-disabled') !== 'true',
      focused: document.activeElement === e,
      checked: checkable ? e.checked : (e.hasAttribute('aria-checked') ? e.getAttribute('aria-checked') === 'true' : null),
      attributes: {
        tag: e.tagName.toLowerCase(),
        type: (e.getAttribute('type') || (e.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase(),
        href: e.getAttribute('href') || '',
        form: form ? 'True' : '',
        form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : '',
        form_has_password: form && form.querySelector('input[type=password]') ? 'True' : '',
        class: (e.getAttribute('class') || '').slice(0, 120),
        autocomplete: e.getAttribute('autocomplete') || '',
        download: e.hasAttribute('download') ? 'True' : '',
        name: e.getAttribute('name') || '',
        dom_id: (e.id || '').slice(0, 80),
        testid: (e.getAttribute('data-testid') || e.getAttribute('data-test') || e.getAttribute('data-cy') || '').slice(0, 80),
        placeholder: (e.getAttribute('placeholder') || '').slice(0, 80),
      },
      bounds: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    });
    if (n >= 400) break;
  }
  return {title: document.title, url: location.href, elements: out,
          text: inner(document.body).slice(0, 6000), ready: document.readyState};
})()
"""

ACT_JS = r"""
((id, action, arg) => {
  const e = document.querySelector(`[data-highhx-id="${id}"]`);
  if (!e) return {found: false};
  e.scrollIntoView({block: 'center', inline: 'center'});
  if (action === 'focus') { e.focus(); return {found: true}; }
  if (action === 'locate') {
    const r = e.getBoundingClientRect();
    return {found: true, x: r.left + r.width / 2, y: r.top + r.height / 2};
  }
  if (action === 'click') {
    e.focus({preventScroll: true}); e.click();
    const link = e.closest('a[href]');
    return {found: true, href: link ? link.href : ''};
  }
  if (action === 'clear') {
    e.focus();
    if ('value' in e) { e.value = ''; e.dispatchEvent(new Event('input', {bubbles: true})); }
    else if (e.isContentEditable) { e.textContent = ''; }
    return {found: true};
  }
  if (action === 'select') {
    const opt = Array.from(e.options || []).find(o => o.value === arg || o.text.trim() === arg);
    if (!opt) return {found: true, error: 'no such option'};
    e.value = opt.value;
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
    return {found: true};
  }
  return {found: true, error: 'unknown action'};
})
"""

KEY_CODES = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "space": (" ", "Space", 32, " "),
}


def find_browser() -> str | None:
    override = os.environ.get("HIGHHX_BROWSER")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    if sys.platform == "darwin":
        return next((p for p in MAC_BROWSERS if Path(p).exists()), None)
    if sys.platform.startswith("win"):
        return next((p for p in WINDOWS_BROWSERS if Path(p).exists()), None)
    return next((found for name in LINUX_BROWSERS if (found := shutil.which(name))), None)


log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3
"""Connection attempts (with backoff) before a browser action fails with a clear error."""
RECOVERY_ATTEMPTS = 3
"""Attempts an action that is safe to retry gets when the connection, tab or page fails."""
NAVIGATE_ATTEMPTS = RECOVERY_ATTEMPTS
NAVIGATE_TIMEOUT = 45.0
"""Seconds Page.navigate may take to answer (it answers once the response headers arrive)."""
SETUP_TIMEOUT = 5.0
"""Seconds each session set-up command may take; past that the tab has a stalled load, which is stopped."""
READY_TIMEOUT = 5.0
"""Seconds one readiness check may take while waiting for a page."""
PING_AFTER = 10.0
"""A connection silent for this long is checked (Browser.getVersion) before it is reused."""
PING_TIMEOUT = 3.0
NEW_TAB_GRACE = 2.0
"""Seconds a new tab opened by a click gets to show where it is going."""
NAVIGATION_GRACE = 0.5
"""Seconds an action gets to start a navigation (form submit, link) before the page counts as settled."""
SUBFRAME_GRACE = 3.0
"""How long to wait for iframes (ads, embeds) once the page itself is complete."""
DOWNLOAD_GRACE = 3.0
"""Seconds a click or navigation gets to start a download."""
DOWNLOAD_TIMEOUT = 600.0
TRANSIENT_NETWORK_ERRORS = frozenset(
    {
        "net::ERR_TIMED_OUT",
        "net::ERR_CONNECTION_TIMED_OUT",
        "net::ERR_CONNECTION_RESET",
        "net::ERR_CONNECTION_CLOSED",
        "net::ERR_EMPTY_RESPONSE",
        "net::ERR_NETWORK_CHANGED",
        "net::ERR_NETWORK_IO_SUSPENDED",
        "net::ERR_HTTP2_PROTOCOL_ERROR",
    }
)
"""Network failures that say nothing about the site: an idempotent navigation is tried again."""
NETWORK_BACKOFF = 1.0
RESET_HINT = "Run `highhx computer browser stop` to reset the HighhX browser, then try again."

_AUTH_HOSTS = frozenset(
    {
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "appleid.apple.com",
        "github.com",
        "gitlab.com",
        "www.facebook.com",
        "api.twitter.com",
        "x.com",
    }
)
_AUTH_HOST_PREFIXES = ("login.", "auth.", "accounts.", "sso.", "id.", "signin.")
_AUTH_PATH = re.compile(r"/(o/)?(oauth2?|authorize|auth|signin|sign-in|login|sso|saml)(/|\b)", re.IGNORECASE)

T = TypeVar("T")


class BrowserState(StrEnum):
    """Where the browser connection stands; every transition is journaled (and audited)."""

    STOPPED = "stopped"
    CONNECTED = "connected"
    PAGE_VALID = "page_valid"
    PAGE_CLOSED = "page_closed"
    TARGET_CHANGED = "target_changed"
    CONNECTION_LOST = "connection_lost"
    BROWSER_CRASHED = "browser_crashed"
    BROWSER_HUNG = "browser_hung"
    NAVIGATION_IN_PROGRESS = "navigation_in_progress"
    NAVIGATION_FAILED = "navigation_failed"
    RECOVERY_REQUIRED = "recovery_required"


class Retry(StrEnum):
    """Whether an action may be performed again after a failure whose outcome is unknown."""

    SAFE = "safe"
    """Reads, waits, screenshots, exact scrolls, history jumps, navigation (verified first)."""
    UNSAFE = "unsafe"
    """Clicks, typing, keys, submits, uploads, downloads: repeating could do it twice."""


@dataclass
class NavigationResult:
    requested: str
    url: str
    """Where the page actually is afterwards (observed, not assumed)."""
    tab: str
    settled: bool = True
    attempts: int = 1
    reused_tab: bool = False
    new_tab: bool = False
    """Opened in a new tab because the working tab showed another site (kept open)."""
    download: dict[str, Any] | None = None
    """The URL was a file: it was downloaded instead of opened."""

    @property
    def redirected(self) -> bool:
        return bool(self.url) and not same_document(self.requested, self.url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "url": self.url,
            "tab": self.tab,
            "redirected": self.redirected,
            "settled": self.settled,
            "attempts": self.attempts,
            "reused_tab": self.reused_tab,
            "new_tab": self.new_tab,
            "download": self.download,
        }


@dataclass
class Download:
    guid: str
    url: str
    filename: str
    path: str = ""
    """The final file, once complete."""
    state: str = "in_progress"
    """in_progress | completed | canceled"""
    received: int = 0
    total: int = 0
    started: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "filename": self.filename,
            "path": self.path,
            "state": self.state,
            "bytes": self.received,
        }


class ChromeBrowser:
    """A HighhX-controlled Chromium-family browser (the BrowserAutomationProvider).

    One connection to the browser endpoint serves every tab; the tab registry is kept current
    from the browser's target events; every action runs through :meth:`_run`, the single place
    where failures are classified, recorded and recovered from according to the action's
    :class:`Retry` class.
    """

    name = "browser"
    reset_hint = RESET_HINT

    def __init__(self, state_dir: Path, *, headless: bool | None = None, binary: str | None = None) -> None:
        self.state_dir = state_dir
        self.state_file = state_dir / "browser.json"
        self.profile_dir = state_dir / "browser-profile"
        self.binary = binary or find_browser()
        self.headless = headless if headless is not None else _no_display()
        self._conn: CDPConnection | None = None
        self._pid = 0
        self.tabs = TabRegistry()
        self._target_id = ""
        """The tab HighhX works in (a stable DevTools target id)."""
        self._session_id = ""
        self._mark: tuple[str, int, int] | None = None
        self._tabs_before: set[str] | None = None
        """Tabs that existed before a click, to recognise the tab the click opened."""
        self._clicked_href = ""
        """The address of the link the last click activated ('' when it was not a link)."""
        self.state = BrowserState.STOPPED
        self.journal: list[dict[str, Any]] = []
        """Recovery, tab, dialog and download events not yet written to the audit trail."""
        self.downloads: dict[str, Download] = {}
        from highhx.computer.network import NetworkJournal

        self.network = NetworkJournal()
        """Requests the pages made (sanitized: no headers, bodies or query values), for verification."""
        self.reconnects = 0
        self._interrupted = False
        """An operation was cancelled mid-way: its load may still be pending in the tab."""
        self.last_wait_settled = True
        """Whether the last wait for the page ended because it settled (False: it timed out)."""

    # --------------------------------------------------------------- journal
    def _note(self, state: BrowserState | None, event: str, detail: str, **extra: Any) -> None:
        if state is not None:
            self.state = state
        self.journal.append({"event": event, "state": str(self.state), "detail": detail, **extra})
        del self.journal[:-200]
        log.info("browser %s (%s): %s", event, self.state, detail)

    def drain_journal(self) -> list[dict[str, Any]]:
        """Events since the last drain (dialog decisions included), for the audit trail."""
        if self._conn is not None:
            for session in self._conn.sessions.values():
                for dialog in session.dialogs:
                    decision = "accepted" if dialog["accepted"] else "dismissed"
                    self._note(
                        None,
                        "dialog",
                        f"{decision} a {dialog['type']} dialog: {dialog['message']!r}",
                        dialog=dialog["type"],
                        accepted=dialog["accepted"],
                    )
                session.dialogs.clear()
        entries, self.journal = self.journal, []
        return entries

    # ----------------------------------------------------------------- state
    def capability(self) -> Capability:
        if not self.binary:
            return Capability(self.name, False, "no Chrome, Chromium, Edge or Brave found (set HIGHHX_BROWSER)")
        return Capability(
            self.name, True, f"{Path(self.binary).name} via DevTools ({'headless' if self.headless else 'visible'})"
        )

    def _saved(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _state(self) -> dict[str, Any] | None:
        data = self._saved()
        if not data or not _alive(int(data.get("pid") or 0)):
            return None
        try:
            self._devtools(int(data["port"]), "/json/version")
        except (OSError, ValueError, KeyError):
            return None
        return data

    @property
    def running(self) -> bool:
        return self._state() is not None

    def start(self, *, cancel: CancellationToken | None = None) -> dict[str, Any]:
        state = self._state()
        if state is not None:
            return state
        previous = self._saved()
        if previous.get("pid") and not _alive(int(previous["pid"])):
            if self.state is not BrowserState.BROWSER_CRASHED:  # not already recorded by the connection
                self._note(BrowserState.BROWSER_CRASHED, "browser_gone", "the browser had exited; starting it again")
        elif self._stop_unresponsive():
            self._note(BrowserState.BROWSER_HUNG, "browser_hung", "the browser stopped responding; restarted it")
        if not self.binary:
            raise ToolNotFoundError(
                "chrome", purpose="automate the browser", hint="Install Google Chrome or set HIGHHX_BROWSER."
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port_file = self.profile_dir / "DevToolsActivePort"
        with contextlib.suppress(OSError):
            port_file.unlink()
        args = [
            self.binary,
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,OptimizationHints,MediaRouter",
            "--autoplay-policy=no-user-gesture-required",  # "play …" starts media in HighhX's own profile
            "--disable-background-networking",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        if self.headless:
            args += ["--headless=new", "--window-size=1280,900"]
        args.append("about:blank")
        process = subprocess.Popen(  # nosec B603 - fixed argv, no shell
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cancel is not None and cancel.cancelled:
                _terminate(process.pid)
                raise OperationCancelledError("Browser start cancelled.")
            if process.poll() is not None:
                raise IntegrationError(
                    f"The browser exited during start-up (code {process.returncode}).",
                    hint="Another browser may be using the HighhX profile; close it (or run "
                    "`highhx computer browser stop`) and try again.",
                )
            try:
                port = int(port_file.read_text().splitlines()[0])
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        else:
            _terminate(process.pid)
            raise IntegrationError("The browser did not open its DevTools port within 30s.")
        state = {"pid": process.pid, "port": port, "headless": self.headless, "binary": self.binary}
        atomic_write_text(self.state_file, json.dumps(state), mode=0o600)
        return state

    def stop(self) -> bool:
        state = self._state()
        self.close()
        self._target_id = ""
        self.tabs = TabRegistry()
        with contextlib.suppress(OSError):
            self.state_file.unlink()
        self.state = BrowserState.STOPPED
        if state is None:
            return self._stop_unresponsive()
        if not self._close_gracefully(state):
            _terminate(int(state["pid"]))
        return True

    def _close_gracefully(self, state: dict[str, Any], timeout: float = 10.0) -> bool:
        """Ask the browser to quit (``Browser.close``): it writes cookies, storage and preferences to
        the profile first. A signal can lose a sign-in made seconds before. False: it did not exit."""
        pid = int(state.get("pid") or 0)
        try:
            version = self._devtools(int(state["port"]), "/json/version") or {}
            conn = CDPConnection(str(version["webSocketDebuggerUrl"]))
        except (OSError, ValueError, KeyError, IntegrationError):
            return False
        try:
            with contextlib.suppress(Exception):  # the connection drops as the browser exits
                conn.call("Browser.close", timeout=5)
        finally:
            conn.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)  # reap it when it is our child
            if not _alive(pid):
                return True
            time.sleep(0.1)
        return False

    def _stop_unresponsive(self) -> bool:
        """A browser HighhX started that is still running but no longer answers DevTools (hung,
        or its port is gone) holds the profile lock, so a new one would exit at once. Stop it —
        only when the process really is HighhX's browser on HighhX's profile (never a reused pid)."""
        try:
            pid = int(self._saved().get("pid") or 0)
        except (ValueError, TypeError):
            return False
        if not _alive(pid) or not _uses_profile(pid, self.profile_dir):
            return False
        _terminate(pid)
        with contextlib.suppress(OSError):
            self.state_file.unlink()
        return True

    def _save_tab(self, target: str) -> None:
        """Remember the working tab so the next `highhx` invocation continues in it."""
        data = self._saved()
        if data and data.get("tab") != target:
            data["tab"] = target
            with contextlib.suppress(OSError):
                atomic_write_text(self.state_file, json.dumps(data), mode=0o600)

    # ------------------------------------------------------------ connection
    def _devtools(self, port: int, path: str, *, method: str = "GET") -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        with urllib.request.urlopen(request, timeout=5) as r:  # nosec B310 - local DevTools endpoint
            body = r.read()
        return json.loads(body) if body.strip().startswith((b"{", b"[")) else None

    def _browser(self, cancel: CancellationToken | None) -> CDPConnection:
        """The healthy browser connection: reused while it answers, rebuilt (restarting the
        browser when needed) when it does not — with a bounded number of attempts."""
        conn = self._conn
        if conn is not None and conn.usable:
            if time.monotonic() - conn.last_answer < PING_AFTER or self._ping(conn, cancel):
                return conn
        if conn is not None:
            if self._pid and not _alive(self._pid):
                self._note(BrowserState.BROWSER_CRASHED, "browser_gone", "the browser process exited; restarting it")
            else:
                self._note(
                    BrowserState.CONNECTION_LOST, "connection_lost", "the browser connection was lost; reconnecting"
                )
            self.close()
        last: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            if cancel is not None and cancel.cancelled:
                raise OperationCancelledError("Browser operation cancelled.")
            try:
                self._conn = self._connect(cancel)
                return self._conn
            except (OSError, ValueError, KeyError, IntegrationError) as exc:
                last = exc
                self.close()
                if attempt + 1 < CONNECT_ATTEMPTS and self._pause(cancel, 0.25 * (2**attempt)):
                    raise OperationCancelledError("Browser operation cancelled.") from None
        detail = last.message if isinstance(last, IntegrationError) else str(last)
        self._note(BrowserState.RECOVERY_REQUIRED, "connect_failed", detail)
        raise IntegrationError(
            f"Could not connect to the browser after {CONNECT_ATTEMPTS} attempts: {detail}", hint=self.reset_hint
        )

    def _ping(self, conn: CDPConnection, cancel: CancellationToken | None) -> bool:
        try:
            conn.call("Browser.getVersion", cancel=cancel, timeout=PING_TIMEOUT)
        except IntegrationError:
            return False
        return True

    def _connect(self, cancel: CancellationToken | None) -> CDPConnection:
        state = self.start(cancel=cancel)
        version = self._devtools(int(state["port"]), "/json/version") or {}
        conn = CDPConnection(str(version["webSocketDebuggerUrl"]))
        try:
            conn.listeners.append(self._on_event)
            conn.call("Target.setDiscoverTargets", {"discover": True}, cancel=cancel, timeout=SETUP_TIMEOUT)
            infos = conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or []
            self.tabs.sync(infos)
            with contextlib.suppress(CDPError, OSError):  # else downloads keep the browser default
                self.downloads_dir.mkdir(parents=True, exist_ok=True)
                conn.call(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allowAndName", "downloadPath": str(self.downloads_dir), "eventsEnabled": True},
                    cancel=cancel,
                    timeout=SETUP_TIMEOUT,
                )
        except BaseException:
            conn.close()
            raise
        if self._pid:
            self.reconnects += 1  # every connection after the first is a recovery
        self._pid = int(state["pid"])
        self._session_id = ""  # sessions belonged to the old connection
        self._note(BrowserState.CONNECTED, "connected", f"connected to the browser (pid {self._pid})")
        return conn

    @property
    def downloads_dir(self) -> Path:
        return self.state_dir / "downloads"

    def pump_events(self, seconds: float = 0.2, *, cancel: CancellationToken | None = None) -> None:
        """Process browser events that arrive within ``seconds`` (late network responses after an
        action) without sending anything. Does nothing when not connected."""
        if self._conn is not None and self._conn.usable:
            with contextlib.suppress(Exception):
                self._conn.pump(cancel=cancel, seconds=seconds)

    def close(self) -> None:
        """Close the connection (the browser keeps running for the next command)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._session_id = ""

    # ---------------------------------------------------------------- events
    def _on_event(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if method.startswith("Network."):
            self.network.handle(method, params, str(message.get("sessionId") or ""))
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            self.tabs.update(params.get("targetInfo") or {})
        elif method == "Target.targetDestroyed":
            target = str(params.get("targetId") or "")
            if self.tabs.remove(target) is not None and target == self._target_id:
                self._note(BrowserState.PAGE_CLOSED, "page_closed", "the tab HighhX was using was closed")
        elif method == "Target.targetCrashed" and params.get("targetId") == self._target_id:
            self._note(BrowserState.RECOVERY_REQUIRED, "page_crashed", "the page crashed")
        elif method == "Browser.downloadWillBegin":
            download = Download(
                str(params.get("guid") or ""), str(params.get("url") or ""), str(params.get("suggestedFilename") or "")
            )
            self.downloads[download.guid] = download
            self._note(None, "download_started", f"downloading {download.filename or download.url}")
        elif method == "Browser.downloadProgress":
            self._download_progress(params)

    def _download_progress(self, params: dict[str, Any]) -> None:
        download = self.downloads.get(str(params.get("guid") or ""))
        if download is None or download.state != "in_progress":
            return
        download.received = int(params.get("receivedBytes") or download.received)
        download.total = int(params.get("totalBytes") or download.total)
        state = str(params.get("state") or "")
        if state == "completed":
            download.path = str(self._finish_download(download))
            download.state = "completed"
            self._note(None, "download_completed", f"downloaded {download.path}", bytes=download.received)
        elif state == "canceled":
            download.state = "canceled"
            self._note(None, "download_canceled", f"the download of {download.filename or download.url} was canceled")

    def _finish_download(self, download: Download) -> Path:
        """The browser saved the file under its guid; give it its own name (never overwriting)."""
        source = self.downloads_dir / download.guid
        name = Path(download.filename or "download").name or "download"
        target = self.downloads_dir / name
        stem, suffix = Path(name).stem, Path(name).suffix
        counter = 1
        while target.exists():
            target = self.downloads_dir / f"{stem} ({counter}){suffix}"
            counter += 1
        try:
            source.rename(target)
        except OSError:
            return source
        return target

    # ------------------------------------------------------------------ tabs
    def _page(self, cancel: CancellationToken | None) -> tuple[CDPConnection, str]:
        """The live session in the working tab — choosing, creating or re-attaching as needed."""
        conn = self._browser(cancel)
        with contextlib.suppress(WebSocketClosed):
            conn.pump(cancel=cancel)  # tab closed / crashed events that already arrived
        if not conn.usable:  # ... or the connection itself turned out to be gone
            conn = self._browser(cancel)
        if self._interrupted and self._target_id in self.tabs.tabs:
            # the last operation was cancelled (Ctrl+C): stop whatever load it left behind, or the
            # tab would hold every command of this session until that load gives up
            self._interrupted = False
            target = self._target_id
            self._forget_session()
            self._target_id, self._session_id = target, self._attach(conn, target, cancel, stop_loading=True)
            return conn, self._session_id
        self._interrupted = False
        for _ in range(RECOVERY_ATTEMPTS):
            session = conn.sessions.get(self._session_id) if self._session_id else None
            if session is not None and not session.closed and not session.crashed and self._target_id in self.tabs.tabs:
                return conn, self._session_id
            crashed = session is not None and session.crashed
            target = self._choose_tab(conn, crashed=crashed, cancel=cancel)
            try:
                return conn, self._attach_target(conn, target, cancel)
            except CDPError as exc:
                if not exc.target_gone:
                    raise
                # it closed between the listing and the attach: forget it and choose again
                self.tabs.remove(target)
                self._session_id = ""
                self._note(BrowserState.PAGE_CLOSED, "page_closed", "the tab closed while HighhX was attaching")
        raise IntegrationError("Could not find a usable tab in the browser.", hint=self.reset_hint)

    def _choose_tab(self, conn: CDPConnection, *, crashed: bool, cancel: CancellationToken | None) -> str:
        previous = self._target_id or str(self._saved().get("tab") or "")
        if crashed and previous:
            with contextlib.suppress(IntegrationError):  # a crashed renderer cannot be trusted again
                conn.call("Target.closeTarget", {"targetId": previous}, cancel=cancel, timeout=SETUP_TIMEOUT)
            self.tabs.remove(previous)
            self._note(BrowserState.RECOVERY_REQUIRED, "page_replaced", "closed the crashed tab")
        if previous in self.tabs.tabs:
            return previous
        if self._target_id and self.state is not BrowserState.PAGE_CLOSED:
            self._note(BrowserState.PAGE_CLOSED, "page_closed", "the tab HighhX was using is gone")
        tab = self.tabs.best()
        if tab is not None:
            if self._target_id:
                self._note(
                    BrowserState.TARGET_CHANGED, "tab_changed", f"continuing in the tab at {tab.url}", tab=tab.id
                )
            return tab.id
        target = self._create_tab(conn, "about:blank", cancel)
        if self._target_id:
            self._note(BrowserState.TARGET_CHANGED, "tab_created", "no tab was left; opened a new one", tab=target)
        return target

    def _create_tab(self, conn: CDPConnection, url: str, cancel: CancellationToken | None) -> str:
        result = conn.call("Target.createTarget", {"url": url}, cancel=cancel, timeout=SETUP_TIMEOUT)
        target = str(result.get("targetId") or "")
        deadline = time.monotonic() + SETUP_TIMEOUT
        while target not in self.tabs.tabs and time.monotonic() < deadline:
            conn.pump(cancel=cancel, seconds=0.05)  # Target.targetCreated
        if target not in self.tabs.tabs:
            self.tabs.sync(
                conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or []
            )
        if target not in self.tabs.tabs:
            raise IntegrationError("The browser did not open a new tab.")
        return target

    def _attach_target(self, conn: CDPConnection, target: str, cancel: CancellationToken | None) -> str:
        try:
            session_id = self._attach(conn, target, cancel)
        except BrowserTimeoutError:
            # A load that never commits (a server that does not answer, a client that gave up on
            # it) holds the tab's commands; Page.stopLoading is answered by the browser itself.
            self._note(
                BrowserState.NAVIGATION_IN_PROGRESS, "stalled_load", "the tab was stuck loading; stopped that load"
            )
            session_id = self._attach(conn, target, cancel, stop_loading=True)
        self._target_id, self._session_id = target, session_id
        self.tabs.touch(target)
        self._save_tab(target)
        self.state = BrowserState.PAGE_VALID
        return session_id

    def _attach(
        self, conn: CDPConnection, target: str, cancel: CancellationToken | None, *, stop_loading: bool = False
    ) -> str:
        """A new session on ``target`` with the domains and settings HighhX relies on."""
        result = conn.call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}, cancel=cancel, timeout=SETUP_TIMEOUT
        )
        session_id = str(result["sessionId"])
        events = conn.track_session(session_id, target)
        try:
            if stop_loading:
                conn.call("Page.stopLoading", session_id=session_id, cancel=cancel, timeout=SETUP_TIMEOUT)
            for method in ("Page.enable", "Runtime.enable", "Inspector.enable", "Network.enable"):
                conn.call(method, session_id=session_id, cancel=cancel, timeout=SETUP_TIMEOUT)
            tree = conn.call("Page.getFrameTree", session_id=session_id, cancel=cancel, timeout=SETUP_TIMEOUT)
            events.main_frame = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
            with contextlib.suppress(CDPError):
                # keys reach the page even when the window is behind the terminal or its address
                # bar has keyboard focus (otherwise Enter can silently go nowhere)
                conn.call(
                    "Emulation.setFocusEmulationEnabled",
                    {"enabled": True},
                    session_id=session_id,
                    cancel=cancel,
                    timeout=SETUP_TIMEOUT,
                )
        except BaseException:
            events.closed = True
            with contextlib.suppress(BrowserDisconnectedError):
                conn.send("Target.detachFromTarget", {"sessionId": session_id})
            raise
        return session_id

    def _forget_session(self) -> None:
        if self._conn is not None and self._session_id in self._conn.sessions:
            self._conn.sessions[self._session_id].closed = True
            with contextlib.suppress(BrowserDisconnectedError):
                self._conn.send("Target.detachFromTarget", {"sessionId": self._session_id})
        self._session_id = ""

    def list_tabs(self, *, cancel: CancellationToken | None = None) -> list[dict[str, Any]]:
        conn = self._browser(cancel)
        self.tabs.sync(conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or [])
        return [{**t.to_dict(), "active": t.id == self._target_id} for t in self.tabs.pages()]

    def switch_tab(self, target: str, *, cancel: CancellationToken | None = None) -> dict[str, Any]:
        """Work in ``target`` (a tab id, or text in a tab's URL or title) from now on."""
        tabs = self.list_tabs(cancel=cancel)
        match = next((t for t in tabs if t["id"] == target), None) or next(
            (t for t in tabs if target.lower() in f"{t['url']} {t['title']}".lower()), None
        )
        if match is None:
            raise NotFoundError(f"No tab matches {target!r}.", hint="List the tabs to see what is open.")
        self._activate(str(match["id"]), cancel)
        self._note(None, "tab_switched", f"switched to the tab at {match['url']}", tab=match["id"])
        return match

    def _activate(self, target: str, cancel: CancellationToken | None) -> None:
        conn = self._browser(cancel)
        with contextlib.suppress(CDPError):
            conn.call("Target.activateTarget", {"targetId": target}, cancel=cancel, timeout=SETUP_TIMEOUT)
        if target != self._target_id or not self._session_id:
            self._forget_session()
            self._attach_target(conn, target, cancel)

    def new_tab(self, url: str | None = None, *, cancel: CancellationToken | None = None) -> NavigationResult:
        """Open a new tab (optionally at ``url``), verify it exists, and work in it."""
        conn = self._browser(cancel)
        before = set(self.tabs.tabs)
        try:
            target = self._create_tab(conn, "about:blank", cancel)
        except OutcomeUnknownError:
            # never open a second one blindly: adopt the tab if it did open
            self.tabs.sync(
                conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or []
            )
            opened = [t for t in self.tabs.pages() if t.id not in before]
            if len(opened) != 1:
                raise
            target = opened[0].id
        self._note(None, "tab_created", "opened a new tab", tab=target)
        self._activate(target, cancel)
        if url:
            return self.navigate(url, cancel=cancel)
        return NavigationResult("about:blank", "about:blank", target)

    def close_tab(self, *, cancel: CancellationToken | None = None) -> dict[str, Any] | None:
        """Close the working tab; continue in the most recently used remaining tab (if any)."""
        conn, _ = self._page(cancel)
        target = self._target_id
        closed = self.tabs.get(target)
        with contextlib.suppress(CDPError):  # already gone: closing is idempotent
            conn.call("Target.closeTarget", {"targetId": target}, cancel=cancel, timeout=SETUP_TIMEOUT)
        self._forget_session()
        self.tabs.remove(target)
        self._target_id = ""
        self._note(None, "tab_closed", f"closed the tab at {closed.url if closed else target}", tab=target)
        remaining = self.tabs.best()
        if remaining is None:
            return None
        self._activate(remaining.id, cancel)
        return remaining.to_dict()

    # ------------------------------------------------------------- execution
    def _run(
        self, op: str, safety: Retry, fn: Callable[[CDPConnection, str], T], cancel: CancellationToken | None
    ) -> T:
        """Run ``fn`` in the working tab's session with the recovery policy.

        * Not delivered (the connection or tab was gone): recover and run it again — always safe.
        * Delivered, answer lost (timeout, crash, tab closed, connection dropped): recover; a
          :attr:`Retry.SAFE` action runs again, a :attr:`Retry.UNSAFE` one is never repeated —
          HighhX looks at the page and reports what it found.
        * Bounded: after :data:`RECOVERY_ATTEMPTS` it fails with every reason.
        """
        problems: list[str] = []
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            conn, session = self._page(cancel)
            try:
                result = fn(conn, session)
            except OperationCancelledError:
                self._interrupted = True
                raise
            except BrowserDisconnectedError as exc:
                problems.append(f"attempt {attempt}: {exc.message}")
                self._recover(exc)
            except OutcomeUnknownError as exc:
                self._recover(exc)
                if safety is Retry.UNSAFE:
                    raise self._unknown(op, exc, cancel) from None
                problems.append(f"attempt {attempt}: {exc.message}")
            else:
                if self.state != BrowserState.PAGE_VALID:
                    self.state = BrowserState.PAGE_VALID
                self.tabs.touch(self._target_id)
                return result
            if attempt < RECOVERY_ATTEMPTS and self._pause(cancel, 0.2 * (2 ** (attempt - 1))):
                raise OperationCancelledError("Browser operation cancelled.")
        self._note(BrowserState.RECOVERY_REQUIRED, "gave_up", f"could not {op} after {RECOVERY_ATTEMPTS} attempts")
        raise IntegrationError(
            f"Could not {op}: the browser failed {RECOVERY_ATTEMPTS} times.", hint=self.reset_hint, details=problems
        )

    def _recover(self, exc: IntegrationError) -> None:
        """Record what failed and invalidate exactly what it broke; the next attempt rebuilds it."""
        conn = self._conn
        if isinstance(exc, PageCrashedError):
            self._note(BrowserState.RECOVERY_REQUIRED, "page_crashed", exc.message)
            return  # the crashed session makes _page replace the tab
        if isinstance(exc, TargetClosedError):
            self._note(BrowserState.PAGE_CLOSED, "page_closed", exc.message)
            self._session_id = ""
            return
        if isinstance(exc, BrowserTimeoutError):
            # the connection is fine; that tab is not answering (e.g. a stalled load)
            self._note(BrowserState.RECOVERY_REQUIRED, "no_answer", exc.message)
            self._forget_session()
            return
        if conn is not None and conn.usable and isinstance(exc, BrowserDisconnectedError):
            self._note(BrowserState.PAGE_CLOSED, "page_closed", exc.message)
            self._session_id = ""
            return
        if self._pid and not _alive(self._pid):
            self._note(BrowserState.BROWSER_CRASHED, "browser_gone", "the browser process exited; restarting it")
        else:
            self._note(BrowserState.CONNECTION_LOST, "connection_lost", exc.message)
        self.close()

    def _unknown(self, op: str, exc: OutcomeUnknownError, cancel: CancellationToken | None) -> OutcomeUnknownError:
        """An unsafe action whose outcome is unknown: recover, look at the page, report both —
        and never repeat the action."""
        details = [exc.message]
        with contextlib.suppress(IntegrationError):
            details.append(f"after recovering, the page is at {self.current_url(cancel=cancel)}")
        self._note(None, "not_repeated", f"{op} may have happened; not repeated", details=details)
        return type(exc)(
            f"{op[:1].upper()}{op[1:]} may or may not have happened: {exc.message}",
            hint="HighhX recovered the browser and did not repeat it; observe the page and decide again.",
            details=details,
        )

    def _eval_in(
        self,
        conn: CDPConnection,
        session: str,
        expression: str,
        cancel: CancellationToken | None,
        *,
        user_gesture: bool = False,
        timeout: float | None = None,
    ) -> Any:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if user_gesture:
            params["userGesture"] = True  # like a person's click: a target=_blank link may open its tab
        result = conn.call("Runtime.evaluate", params, session_id=session, cancel=cancel, timeout=timeout)
        if result.get("exceptionDetails"):
            raise IntegrationError(f"Page script failed: {result['exceptionDetails'].get('text')}")
        return (result.get("result") or {}).get("value")

    def _eval(self, expression: str, cancel: CancellationToken | None, *, safety: Retry = Retry.SAFE) -> Any:
        return self._run(
            "run a page script", safety, lambda conn, session: self._eval_in(conn, session, expression, cancel), cancel
        )

    def current_url(self, *, cancel: CancellationToken | None = None) -> str:
        return str(self._eval("location.href", cancel) or "")

    def _before_action(self, cancel: CancellationToken | None, *, follow_tabs: bool = False) -> None:
        """Remember the navigation count so ``wait_ready`` can tell whether the action started one
        (and, for clicks, which tabs exist so a tab the click opens is recognised)."""
        conn, session = self._page(cancel)
        events = conn.sessions[session]
        self._mark = (session, events.navigations, events.settled)
        self._tabs_before = set(self.tabs.tabs) if follow_tabs else None
        self._clicked_href = ""

    @staticmethod
    def _pause(cancel: CancellationToken | None, seconds: float = 0.05) -> bool:
        if cancel is not None:
            return cancel.wait(seconds)
        time.sleep(seconds)
        return False

    # ------------------------------------------------------------ navigation
    def navigate(
        self, url: str, *, cancel: CancellationToken | None = None, reuse_tab: bool = False
    ) -> NavigationResult:
        """Open ``url`` in the working tab and wait for it — or, with ``reuse_tab``, switch to a
        tab that already shows it.

        Opening a URL is idempotent, so after a failure it is issued again — but only after
        recovering and looking at where the page is: if it already got there it is not
        requested again. The result is what the browser shows afterwards, never an assumption."""
        if reuse_tab:
            reused = self._open_where(url, cancel)
            if reused is not None:
                return reused
        problems: list[str] = []
        before = set(self.downloads)
        for attempt in range(1, NAVIGATE_ATTEMPTS + 1):
            conn, session = self._page(cancel)
            try:
                if attempt > 1:
                    here = str(self._eval_in(conn, session, "location.href", cancel, timeout=SETUP_TIMEOUT) or "")
                    if arrived(url, here):
                        self._note(None, "navigation_confirmed", f"the page had already reached {here}")
                        self.wait_ready(cancel=cancel)
                        break
                events = conn.sessions[session]
                self._mark = (session, events.navigations, events.settled)
                self.state = BrowserState.NAVIGATION_IN_PROGRESS
                answer = conn.call(
                    "Page.navigate", {"url": url}, session_id=session, cancel=cancel, timeout=NAVIGATE_TIMEOUT
                )
                error = str(answer.get("errorText") or "")
                if error == "net::ERR_ABORTED":  # replaced by a redirect — or it is a file
                    download = self._await_download(before, cancel)
                    if download is not None:
                        self._mark = None
                        self.state = BrowserState.PAGE_VALID
                        return NavigationResult(
                            url, self.current_url(cancel=cancel), self._target_id, download=download.to_dict()
                        )
                elif error in TRANSIENT_NETWORK_ERRORS and attempt < NAVIGATE_ATTEMPTS:
                    # a network hiccup, not an answer about the site: opening is idempotent, try again
                    problems.append(f"attempt {attempt}: {error}")
                    self._note(None, "network_retry", f"{url}: {error}; trying again")
                    if self._pause(cancel, NETWORK_BACKOFF * attempt):
                        raise OperationCancelledError("Browser operation cancelled.")
                    continue
                elif error:
                    self._note(BrowserState.NAVIGATION_FAILED, "navigation_failed", f"{url}: {error}")
                    raise NavigationError(f"Could not open {url}: {error}", hint=_network_hint(error), details=problems)
                self.wait_ready(cancel=cancel)
                break
            except OperationCancelledError:
                self._interrupted = True
                raise
            except BrowserTimeoutError:
                # the connection is fine; the site is not answering. Retrying would only wait again.
                self._note(BrowserState.NAVIGATION_FAILED, "site_timeout", f"{url} did not respond")
                self._abandon_load(cancel)
                raise NavigationError(
                    f"Could not open {url}: the site did not respond within {NAVIGATE_TIMEOUT:.0f}s.",
                    hint="The site may be down or very slow; HighhX stopped loading it. Try again later.",
                ) from None
            except (BrowserDisconnectedError, OutcomeUnknownError) as exc:
                problems.append(f"attempt {attempt}: {exc.message}")
                self._recover(exc)
                if attempt < NAVIGATE_ATTEMPTS and self._pause(cancel, 0.2 * (2 ** (attempt - 1))):
                    raise OperationCancelledError("Browser operation cancelled.") from None
        else:
            self._note(BrowserState.NAVIGATION_FAILED, "gave_up", f"could not open {url}")
            raise IntegrationError(
                f"Could not open {url}: the browser connection failed {NAVIGATE_ATTEMPTS} times.",
                hint="HighhX recovered and checked the page before each retry. " + RESET_HINT,
                details=problems,
            )
        if problems:
            self._note(None, "recovered", f"opened {url} after recovering from: {'; '.join(problems)}")
        landed = self.current_url(cancel=cancel)
        tab = self.tabs.get(self._target_id)
        if tab is not None:
            tab.url = landed or tab.url
            tab.opened(url, landed)
        return NavigationResult(url, landed, self._target_id, settled=self.last_wait_settled, attempts=attempt)

    def _open_where(self, url: str, cancel: CancellationToken | None) -> NavigationResult | None:
        """Where "open <url>" goes, before anything is loaded:

        * a tab already shows it: work there (the working tab is not reloaded, another is switched to)
        * the working tab is blank or on the same site: None — navigate it in place
        * the working tab shows another site: open ``url`` in a new tab, keeping that page
        """
        conn, _ = self._page(cancel)  # a live working tab (recreated if it was closed)
        with contextlib.suppress(CDPError, OSError):  # the registry is event-driven; confirm it once
            self.tabs.sync(
                conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or []
            )
        existing = self.tabs.find(url)
        if existing is not None:
            if existing.id == self._target_id:
                self._note(None, "already_open", f"{url} is already open in the working tab", tab=existing.id)
            else:
                self._activate(existing.id, cancel)
                self._note(None, "tab_reused", f"{url} was already open; switched to that tab", tab=existing.id)
            self.wait_ready(cancel=cancel)
            return NavigationResult(url, self.current_url(cancel=cancel), existing.id, reused_tab=True)
        working = self.tabs.get(self._target_id)
        if working is None or _replaceable(working.url, url):
            return None
        kept = working.url
        result = self.new_tab(url, cancel=cancel)
        result.new_tab = True
        self._note(None, "opened_beside", f"opened {url} in a new tab; {kept} stays open", tab=result.tab)
        return result

    def _abandon_load(self, cancel: CancellationToken | None) -> None:
        """Stop a load that will not finish, so the tab answers the next command at once."""
        target = self._target_id
        self._forget_session()
        with contextlib.suppress(IntegrationError):
            conn = self._browser(cancel)
            if target in self.tabs.tabs:
                self._target_id, self._session_id = target, self._attach(conn, target, cancel, stop_loading=True)

    def wait_ready(self, *, timeout: float = 30.0, cancel: CancellationToken | None = None) -> None:
        """Wait until the page settled after the last action.

        ``document.readyState`` alone is not enough: right after a click or Enter the *old*
        document is still "complete" for a moment before the navigation it triggered starts.
        So first give the action a short grace period to start a navigation, then wait until
        no frame is loading and the (new) document is complete.

        A slow page is not an error: after ``timeout`` the wait ends and
        :attr:`last_wait_settled` is False, so callers observe whatever has loaded. A crashed
        page is an error (:class:`PageCrashedError`); anything else is recovered from.
        """
        deadline = time.monotonic() + timeout
        mark, self._mark = self._mark, None
        tabs_before, self._tabs_before = self._tabs_before, None
        navigating: tuple[str, int] | None = None
        if mark is not None and self._conn is not None:
            session_id, before, settled = mark
            events = self._conn.sessions.get(session_id)
            grace = time.monotonic() + NAVIGATION_GRACE
            while events is not None and not events.closed and events.navigations == before:
                if time.monotonic() >= grace or self._conn is None:
                    break
                try:
                    self._conn.pump(cancel=cancel, seconds=0.05)
                except WebSocketClosed:
                    break
            if events is not None and events.navigations != before:
                navigating = (session_id, settled)  # the page itself moved on: stay (a popup beside it is an ad)
            elif tabs_before is not None:
                self._follow_new_tab(tabs_before, cancel)
        page_complete_at: float | None = None
        self.last_wait_settled = False
        while time.monotonic() < deadline:
            try:
                conn, session = self._page(cancel)
                events = conn.sessions[session]
                state = self._eval_in(conn, session, "document.readyState", cancel, timeout=READY_TIMEOUT)
                complete = state == "complete" and not events.main_frame_loading
                if navigating is not None and navigating[0] == session:
                    complete = complete and events.settled > navigating[1]  # the navigation itself finished
                if complete:
                    page_complete_at = page_complete_at or time.monotonic()
                    # iframes (ads, embeds) get a short grace, not the whole timeout
                    if not events.loading_frames or time.monotonic() - page_complete_at >= SUBFRAME_GRACE:
                        self.last_wait_settled = True
                        self.state = BrowserState.PAGE_VALID
                        return
                else:
                    page_complete_at = None
            except PageCrashedError:
                raise
            except OperationCancelledError:
                self._interrupted = True
                raise
            except (BrowserDisconnectedError, OutcomeUnknownError) as exc:
                self._recover(exc)
            except IntegrationError:
                pass  # e.g. the document is being replaced
            if self._pause(cancel):
                self._interrupted = True
                raise OperationCancelledError("Browser operation cancelled.")

    def _follow_new_tab(self, before: set[str], cancel: CancellationToken | None) -> None:
        """Continue in a tab the last click opened — only when it is the one the person meant:
        opened by the working tab, and either the clicked link's own address (target=_blank) or
        a sign-in window. Anything else (ads, trackers, unrelated windows) is left alone."""
        opener = self._target_id
        conn = self._conn
        deadline = time.monotonic() + NEW_TAB_GRACE
        while conn is not None and time.monotonic() < deadline:
            opened = self.tabs.opened_by(opener, since=before)
            if not opened:
                with contextlib.suppress(WebSocketClosed):
                    conn.pump(cancel=cancel, seconds=0.1)
                if not self.tabs.opened_by(opener, since=before):
                    return  # the click opened nothing
                continue
            chosen = [t for t in opened if self._intended(t.url)]
            if len(chosen) == 1:
                self._activate(chosen[0].id, cancel)
                self._note(
                    BrowserState.TARGET_CHANGED,
                    "tab_followed",
                    f"the click opened {chosen[0].url} in a new tab; continuing there",
                    tab=chosen[0].id,
                )
                return
            if all(t.url not in ("", "about:blank") for t in opened):
                for tab in opened:
                    self._note(None, "popup_ignored", f"ignored a tab the page opened: {tab.url}", tab=tab.id)
                return
            with contextlib.suppress(WebSocketClosed):
                conn.pump(cancel=cancel, seconds=0.1)  # a new tab reports about:blank until it starts loading

    def _intended(self, url: str) -> bool:
        """The clicked link's own address, or a sign-in window (OAuth/SSO) the click opened."""
        if self._clicked_href and same_document(self._clicked_href, url):
            return True
        return is_sign_in_window(url)

    def _await_download(self, before: set[str], cancel: CancellationToken | None) -> Download | None:
        """A download that started since ``before`` (waiting briefly for it to start), followed
        to completion — so a file is never mistaken for a page."""
        conn = self._conn
        deadline = time.monotonic() + DOWNLOAD_GRACE
        started: Download | None = None
        while conn is not None and started is None and time.monotonic() < deadline:
            started = next((d for guid, d in self.downloads.items() if guid not in before), None)
            if started is None:
                conn.pump(cancel=cancel, seconds=0.1)
        if started is None:
            return None
        return self.wait_download(started, cancel=cancel)

    def wait_download(
        self, download: Download, *, timeout: float = DOWNLOAD_TIMEOUT, cancel: CancellationToken | None = None
    ) -> Download:
        """Follow a download to completion (the connection may be rebuilt meanwhile)."""
        deadline = time.monotonic() + timeout
        while download.state == "in_progress":
            if time.monotonic() > deadline:
                raise IntegrationError(f"The download of {download.filename or download.url} did not finish in time.")
            try:
                self._browser(cancel).pump(cancel=cancel, seconds=0.2)
            except WebSocketClosed:
                continue
            if download.state == "in_progress" and self._conn is not None and self.reconnects:
                # progress events of a download that began on an older connection may not arrive
                raw = self.downloads_dir / download.guid
                if raw.exists() and time.monotonic() - download.started > 2 and self._stable(raw):
                    self._download_progress({"guid": download.guid, "state": "completed"})
        if download.state == "canceled":
            raise IntegrationError(f"The download of {download.filename or download.url} was canceled.")
        return download

    @staticmethod
    def _stable(path: Path) -> bool:
        size = path.stat().st_size
        time.sleep(0.5)
        return path.exists() and path.stat().st_size == size

    # ----------------------------------------------------------------- history
    def go_back(self, *, cancel: CancellationToken | None = None) -> str:
        return self._history(-1, cancel)

    def go_forward(self, *, cancel: CancellationToken | None = None) -> str:
        return self._history(1, cancel)

    def _history(self, delta: int, cancel: CancellationToken | None) -> str:
        """Go to one exact history entry — idempotent, so it is safe to retry."""
        history = self._run(
            "read the history", Retry.SAFE, lambda c, s: c.call("Page.getNavigationHistory", session_id=s), cancel
        )
        index = int(history.get("currentIndex") or 0) + delta
        entries = history.get("entries") or []
        if not 0 <= index < len(entries):
            raise UsageError(f"There is no page to go {'back' if delta < 0 else 'forward'} to.")
        entry = entries[index]
        self._before_action(cancel)
        self._run(
            "go back" if delta < 0 else "go forward",
            Retry.SAFE,
            lambda c, s: c.call("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, session_id=s),
            cancel,
        )
        self.wait_ready(cancel=cancel)
        return str(entry.get("url") or "")

    def reload(self, *, cancel: CancellationToken | None = None) -> None:
        # a page that was the answer to a form would be submitted again: never repeated blindly
        self._before_action(cancel)
        self._run("reload the page", Retry.UNSAFE, lambda c, s: c.call("Page.reload", session_id=s), cancel)
        self.wait_ready(cancel=cancel)

    # ----------------------------------------------------------------- observe
    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        data = self._eval(OBSERVE_JS, cancel) or {}
        elements = [
            UIElement(
                id=str(e["id"]),
                role=str(e["role"]),
                name=str(e.get("name") or ""),
                value=str(e.get("value") or ""),
                enabled=bool(e.get("enabled", True)),
                focused=bool(e.get("focused")),
                checked=e.get("checked"),
                attributes={k: str(v) for k, v in (e.get("attributes") or {}).items() if v not in (None, "")},
                bounds=tuple(e["bounds"]) if e.get("bounds") else None,
                source="dom",
            )
            for e in data.get("elements") or []
        ]
        browser = Path(self.binary).name if self.binary else "browser"
        tab = self.tabs.get(self._target_id)
        if tab is not None:
            tab.url, tab.title = str(data.get("url") or tab.url), str(data.get("title") or tab.title)
        return Observation(
            provider=self.name,
            application=browser,
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            elements=elements,
            text=str(data.get("text") or ""),
            captured_at=time.time(),
        )

    def evaluate(self, expression: str, *, cancel: CancellationToken | None = None, retry_safe: bool = False) -> Any:
        """Evaluate a fixed HighhX script in the page (never text from a model or a page).
        ``retry_safe``: the script only reads, or doing it twice is harmless."""
        return self._eval(expression, cancel, safety=Retry.SAFE if retry_safe else Retry.UNSAFE)

    def screenshot(self, *, cancel: CancellationToken | None = None) -> bytes:
        """The current page as PNG bytes (read-only; nothing on the page changes)."""
        import base64

        data = self._run(
            "take a screenshot",
            Retry.SAFE,
            lambda c, s: c.call("Page.captureScreenshot", {"format": "png"}, session_id=s, cancel=cancel),
            cancel,
        )
        return base64.b64decode(str(data.get("data") or ""))

    # ------------------------------------------------- the page as pixels (vision)
    def viewport(self, *, cancel: CancellationToken | None = None) -> dict[str, Any]:
        """Where the page is: URL, title, scroll offset and viewport size in CSS pixels, pixel ratio."""
        data = self._eval(
            "({url: location.href, title: document.title, x: scrollX, y: scrollY,"
            " width: innerWidth, height: innerHeight, dpr: devicePixelRatio})",
            cancel,
        )
        return dict(data or {})

    def page_capture(self, *, cancel: CancellationToken | None = None) -> tuple[bytes, dict[str, Any]]:
        """The visible viewport as PNG, one image pixel per CSS pixel, with the viewport it shows."""
        import base64

        view = self.viewport(cancel=cancel)
        clip = {
            "x": float(view.get("x") or 0),
            "y": float(view.get("y") or 0),
            "width": float(view.get("width") or 0),
            "height": float(view.get("height") or 0),
            "scale": 1 / float(view.get("dpr") or 1),
        }
        data = self._run(
            "take a screenshot",
            Retry.SAFE,
            lambda c, s: c.call("Page.captureScreenshot", {"format": "png", "clip": clip}, session_id=s, cancel=cancel),
            cancel,
        )
        return base64.b64decode(str(data.get("data") or "")), view

    def pointer(
        self,
        kind: str,
        x: float,
        y: float,
        *,
        to: tuple[float, float] | None = None,
        button: str = "left",
        count: int = 1,
        direction: str = "down",
        cancel: CancellationToken | None = None,
    ) -> None:
        """Pointer input at a viewport point (CSS pixels): click, move, drag, wheel. A click or drag
        is never repeated after a failure (it could happen twice); a move is."""
        if kind in ("click", "drag"):
            self._before_action(cancel, follow_tabs=kind == "click")

        def events(conn: CDPConnection, session: str) -> None:
            def mouse(**event: Any) -> None:
                self._mouse(conn, session, cancel, **event)

            mouse(type="mouseMoved", x=x, y=y)
            if kind == "click":
                for n in range(1, count + 1):
                    mouse(type="mousePressed", x=x, y=y, button=button, clickCount=n)
                    mouse(type="mouseReleased", x=x, y=y, button=button, clickCount=n)
            elif kind == "drag" and to is not None:
                mouse(type="mousePressed", x=x, y=y, button=button, clickCount=1)
                for step in range(1, 9):
                    mouse(type="mouseMoved", x=x + (to[0] - x) * step / 8, y=y + (to[1] - y) * step / 8, button=button)
                mouse(type="mouseReleased", x=to[0], y=to[1], button=button, clickCount=1)
            elif kind == "wheel":
                dx, dy = {"down": (0, 400), "up": (0, -400), "right": (400, 0), "left": (-400, 0)}[direction]
                mouse(type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)

        self._run(f"{kind} at ({x:g}, {y:g})", Retry.SAFE if kind == "move" else Retry.UNSAFE, events, cancel)

    def insert_text(self, text: str, *, cancel: CancellationToken | None = None) -> None:
        """Type into whatever has focus on the page (after a visual click into a field)."""
        self._before_action(cancel)
        self._run(
            "type text",
            Retry.UNSAFE,
            lambda c, s: c.call("Input.insertText", {"text": text}, session_id=s, cancel=cancel),
            cancel,
        )

    def key_combo(self, modifiers: list[str], key: str, *, cancel: CancellationToken | None = None) -> None:
        """A key with modifiers (``["command"], "a"``) to the page."""
        bits = {"option": 1, "control": 2, "command": 4, "shift": 8}
        mask = sum(bits[m] for m in modifiers if m in bits)
        if key in KEY_CODES:
            name, code, vk, text = KEY_CODES[key]
        elif len(key) == 1:
            name, code, vk, text = key, f"Key{key.upper()}", ord(key.upper()), key
        else:
            raise IntegrationError(f"Unsupported key {key!r}")
        down: dict[str, Any] = {"type": "keyDown", "key": name, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mask}
        if text and not mask & (2 | 4):
            down["text"] = text
        up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mask}
        self._before_action(cancel)

        def keystroke(conn: CDPConnection, session: str) -> None:
            conn.call("Input.dispatchKeyEvent", down, session_id=session, cancel=cancel)
            conn.call("Input.dispatchKeyEvent", up, session_id=session, cancel=cancel)

        self._run(f"press {'+'.join([*modifiers, key])}", Retry.UNSAFE, keystroke, cancel)

    # ----------------------------------------------------------------- actions
    def _act(
        self, element_id: str, action: str, arg: str = "", cancel: CancellationToken | None = None
    ) -> dict[str, Any]:
        safety = Retry.SAFE if action in ("focus", "clear", "locate") else Retry.UNSAFE
        if action == "click":
            self._before_action(cancel, follow_tabs=True)
        elif action != "locate":
            self._before_action(cancel)
        script = f"({ACT_JS})({json.dumps(element_id)}, {json.dumps(action)}, {json.dumps(arg)})"
        result = self._run(
            f"{action} {element_id}",
            safety,
            lambda c, s: self._eval_in(c, s, script, cancel, user_gesture=action == "click"),
            cancel,
        )
        if not result or not result.get("found"):
            raise ElementNotFoundError(f"Element {element_id} is no longer on the page.")
        if result.get("error"):
            raise IntegrationError(f"Could not {action} {element_id}: {result['error']}")
        if action == "click":
            self._clicked_href = str(result.get("href") or "")
        return dict(result)

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "click", cancel=cancel)

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "clear", cancel=cancel)
        self._run(
            "type text",
            Retry.UNSAFE,  # inserting twice would duplicate the text
            lambda c, s: c.call("Input.insertText", {"text": text}, session_id=s, cancel=cancel),
            cancel,
        )

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        if key not in KEY_CODES:
            raise IntegrationError(f"Unsupported key {key!r}")
        name, code, vk, text = KEY_CODES[key]
        self._before_action(cancel)
        down: dict[str, Any] = {"type": "keyDown", "key": name, "code": code, "windowsVirtualKeyCode": vk}
        if text:
            down["text"] = text
        up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": vk}

        def keystroke(conn: CDPConnection, session: str) -> None:
            conn.call("Input.dispatchKeyEvent", down, session_id=session, cancel=cancel)
            conn.call("Input.dispatchKeyEvent", up, session_id=session, cancel=cancel)

        self._run(f"press {key}", Retry.UNSAFE, keystroke, cancel)

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        delta = {"down": 600, "up": -600}.get(direction)
        if delta is None:
            raise IntegrationError(f"Unsupported scroll direction {direction!r}")
        # scroll to an exact position (read once), so a retry after a failure cannot scroll twice
        start = float(self._eval("window.scrollY", cancel) or 0)
        self._eval(f"window.scrollTo(0, {max(0.0, start + delta)})", cancel)

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "select", option, cancel=cancel)

    def _center(self, element_id: str, cancel: CancellationToken | None) -> tuple[float, float]:
        found = self._act(element_id, "locate", cancel=cancel)
        return float(found["x"]), float(found["y"])

    def _mouse(self, conn: CDPConnection, session: str, cancel: CancellationToken | None, **event: Any) -> None:
        conn.call("Input.dispatchMouseEvent", event, session_id=session, cancel=cancel)

    def hover(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        x, y = self._center(element_id, cancel)
        self._run(
            f"hover {element_id}",
            Retry.SAFE,  # moving the pointer to the same place twice changes nothing
            lambda c, s: self._mouse(c, s, cancel, type="mouseMoved", x=x, y=y),
            cancel,
        )

    def double_click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        x, y = self._center(element_id, cancel)
        self._before_action(cancel, follow_tabs=True)

        def double(conn: CDPConnection, session: str) -> None:
            self._mouse(conn, session, cancel, type="mouseMoved", x=x, y=y)
            for count in (1, 2):
                for kind in ("mousePressed", "mouseReleased"):
                    self._mouse(conn, session, cancel, type=kind, x=x, y=y, button="left", clickCount=count)

        self._run(f"double-click {element_id}", Retry.UNSAFE, double, cancel)

    def right_click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        """A right-button click on the element (the page's ``contextmenu`` handler, or the browser's
        context menu, which DevTools closes with Escape like any native menu)."""
        x, y = self._center(element_id, cancel)
        self._before_action(cancel)

        def right(conn: CDPConnection, session: str) -> None:
            self._mouse(conn, session, cancel, type="mouseMoved", x=x, y=y)
            for kind in ("mousePressed", "mouseReleased"):
                self._mouse(conn, session, cancel, type=kind, x=x, y=y, button="right", clickCount=1)

        self._run(f"right-click {element_id}", Retry.UNSAFE, right, cancel)

    def drag(self, source_id: str, target_id: str, *, cancel: CancellationToken | None = None) -> None:
        """Drag one element onto another: HTML5 drag and drop (intercepted and replayed at the
        drop target) and pointer-driven drags (mouse events) both work."""
        (sx, sy), (tx, ty) = self._center(source_id, cancel), self._center(target_id, cancel)
        self._before_action(cancel)

        def drag(conn: CDPConnection, session: str) -> None:
            seen = len(conn.events)
            with contextlib.suppress(CDPError):
                conn.call("Input.setInterceptDrags", {"enabled": True}, session_id=session, cancel=cancel)
            self._mouse(conn, session, cancel, type="mouseMoved", x=sx, y=sy)
            self._mouse(conn, session, cancel, type="mousePressed", x=sx, y=sy, button="left", clickCount=1)
            for step in range(1, 11):
                x, y = sx + (tx - sx) * step / 10, sy + (ty - sy) * step / 10
                self._mouse(conn, session, cancel, type="mouseMoved", x=x, y=y, button="left")
            intercepted = next(
                (
                    e
                    for e in conn.events[seen:]
                    if e.get("method") == "Input.dragIntercepted" and e.get("sessionId") == session
                ),
                None,
            )
            if intercepted is not None:
                data = (intercepted.get("params") or {}).get("data") or {}
                for kind in ("dragEnter", "dragOver", "drop"):
                    conn.call(
                        "Input.dispatchDragEvent",
                        {"type": kind, "x": tx, "y": ty, "data": data},
                        session_id=session,
                        cancel=cancel,
                    )
            self._mouse(conn, session, cancel, type="mouseReleased", x=tx, y=ty, button="left", clickCount=1)
            with contextlib.suppress(CDPError):
                conn.call("Input.setInterceptDrags", {"enabled": False}, session_id=session, cancel=cancel)

        self._run(f"drag {source_id} onto {target_id}", Retry.UNSAFE, drag, cancel)

    def upload(self, element_id: str, paths: list[str], *, cancel: CancellationToken | None = None) -> None:
        """Choose ``paths`` in a file input (the caller confines and approves them)."""
        selector = json.dumps(f'[data-highhx-id="{element_id}"]')

        def choose(conn: CDPConnection, session: str) -> None:
            found = conn.call(
                "Runtime.evaluate", {"expression": f"document.querySelector({selector})"}, session_id=session
            )
            handle = (found.get("result") or {}).get("objectId")
            if not handle:
                raise ElementNotFoundError(f"Element {element_id} is no longer on the page.")
            kind = self._eval_in(
                conn, session, f"(document.querySelector({selector}).type || '').toLowerCase()", cancel
            )
            if kind != "file":
                raise UsageError(f"Element {element_id} is not a file field.")
            conn.call("DOM.setFileInputFiles", {"files": paths, "objectId": handle}, session_id=session, cancel=cancel)

        self._before_action(cancel)
        self._run(f"upload to {element_id}", Retry.UNSAFE, choose, cancel)

    def download(self, element_id: str, *, cancel: CancellationToken | None = None) -> Download:
        """Click a link or button that downloads a file and follow the download to completion."""
        before = set(self.downloads)
        self.click(element_id, cancel=cancel)
        download = self._await_download(before, cancel)
        if download is None:
            raise IntegrationError("The click did not start a download.")
        return download


class ElementNotFoundError(IntegrationError):
    """The element from the last observation is gone (the UI changed)."""


class NavigationError(IntegrationError):
    """The site answered with an error (or never answered): the browser is fine; retrying now will not help."""


def is_sign_in_window(url: str) -> bool:
    """A popup for signing in (OAuth / SSO): a known identity host, or a login./auth./… host,
    at a sign-in path. Payment windows are not included: they are followed only when asked for."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    known = host in _AUTH_HOSTS or host.startswith(_AUTH_HOST_PREFIXES)
    return known and bool(_AUTH_PATH.search(parsed.path or ""))


def arrived(requested: str, actual: str) -> bool:
    """The browser is where it was sent (a ``www.``/scheme redirect or a deeper path counts)."""
    a, b = urlparse(requested), urlparse(actual)
    host_a = (a.hostname or "").lower().removeprefix("www.")
    host_b = (b.hostname or "").lower().removeprefix("www.")
    return bool(host_a) and host_a == host_b and (b.path or "/").startswith((a.path or "/").rstrip("/") or "/")


BLANK_PAGES = ("about:blank", "chrome://newtab", "chrome://new-tab-page", "chrome-search://", "edge://newtab")


def _replaceable(current: str, requested: str) -> bool:
    """The working tab may be navigated to ``requested``: it is blank (a new tab page) or already
    on the same site — anything else is a page someone may be using, left open."""
    if not current or current.startswith(BLANK_PAGES):
        return True
    here = (urlparse(current).hostname or "").lower().removeprefix("www.")
    there = (urlparse(requested).hostname or "").lower().removeprefix("www.")
    return bool(here) and here == there


def _no_display() -> bool:
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return bool(os.environ.get("HIGHHX_HEADLESS"))


_NETWORK_HINTS = {
    "net::ERR_NAME_NOT_RESOLVED": "The address does not exist or DNS is unreachable; check the spelling.",
    "net::ERR_INTERNET_DISCONNECTED": "This computer is offline.",
    "net::ERR_CONNECTION_REFUSED": "Nothing is listening at that address (is the server running?).",
    "net::ERR_CONNECTION_TIMED_OUT": "The site did not respond; try again later.",
    "net::ERR_TIMED_OUT": "The site did not respond; try again later.",
    "net::ERR_CERT_AUTHORITY_INVALID": "The site's certificate is not trusted; HighhX does not bypass that.",
    "net::ERR_BLOCKED_BY_CLIENT": "The request was blocked by the browser.",
}


def _network_hint(error: str) -> str:
    if error.startswith("net::ERR_CERT_"):
        return _NETWORK_HINTS["net::ERR_CERT_AUTHORITY_INVALID"]
    return _NETWORK_HINTS.get(error, "Check the address and your connection, then try again.")


def _uses_profile(pid: int, profile: Path) -> bool:
    """``pid`` is a browser started on ``profile`` (guards against a recycled pid)."""
    if sys.platform.startswith("win"):
        return False  # no cheap, reliable command line lookup: never kill on a guess
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f"--user-data-dir={profile}" in out


class RemoteBrowser(ChromeBrowser):
    """A browser HighhX did not start: an existing DevTools endpoint — a browser on another
    computer (``wss://`` or through an ``ssh -L`` tunnel), a hosted browser service, or one you
    started with ``--remote-debugging-port``.

        HIGHHX_BROWSER_ENDPOINT=http://127.0.0.1:9222          # or computer.browser_endpoint
        HIGHHX_BROWSER_ENDPOINT=wss://browser.example.com/devtools/browser/…?token=…

    Everything that runs over the DevTools connection is the same as for HighhX's own browser —
    tabs, navigation, the page's elements, dialogs, screenshots, verification, reconnection after
    a lost connection. What HighhX cannot do to a browser it did not start is refused, not faked:
    it never launches, restarts or stops it, and downloads (saved on the browser's computer, not
    this one) cannot be followed or verified here."""

    reset_hint = "Start the remote browser (HighhX never starts it), or fix HIGHHX_BROWSER_ENDPOINT."

    def __init__(self, state_dir: Path, endpoint: str) -> None:
        super().__init__(state_dir, headless=True, binary="remote")
        self.endpoint = endpoint.rstrip("/")

    @property
    def shown_endpoint(self) -> str:
        return self.endpoint.split("?", 1)[0]  # a token in the query is never shown or logged

    def capability(self) -> Capability:
        return Capability(self.name, True, f"remote browser {self.shown_endpoint} via DevTools")

    def _ws_url(self) -> str:
        from urllib.parse import urlparse

        parts = urlparse(self.endpoint)
        if parts.scheme in ("ws", "wss"):
            return self.endpoint
        # the query (a token) stays a query: http://host:9222/json/version?token=…
        version_url = parts._replace(path=parts.path.rstrip("/") + "/json/version").geturl()
        request = urllib.request.Request(version_url)
        try:
            with urllib.request.urlopen(request, timeout=10) as r:  # nosec B310 - the configured endpoint
                version = json.loads(r.read())
        except (OSError, ValueError) as exc:
            raise IntegrationError(f"The remote browser at {self.shown_endpoint} does not answer: {exc}") from None
        url = str(version.get("webSocketDebuggerUrl") or "")
        if not url:
            raise IntegrationError(f"{self.shown_endpoint} is not a DevTools endpoint.")
        return url

    def _state(self) -> dict[str, Any] | None:
        try:
            self._ws_url()
        except IntegrationError:
            return None
        return {"pid": 0, "port": 0, "endpoint": self.shown_endpoint}

    def start(self, *, cancel: CancellationToken | None = None) -> dict[str, Any]:
        state = self._state()
        if state is None:
            raise IntegrationError(
                f"The remote browser at {self.shown_endpoint} is unreachable.",
                hint="HighhX never starts a remote browser: start it, or fix HIGHHX_BROWSER_ENDPOINT.",
            )
        return state

    def stop(self) -> bool:
        self.close()  # the connection only: the browser is not HighhX's to stop
        self.state = BrowserState.STOPPED
        return True

    def _connect(self, cancel: CancellationToken | None) -> CDPConnection:
        from urllib.parse import urlparse

        url = self._ws_url()
        remote = (urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1")
        conn = CDPConnection(url, allow_remote=remote)
        try:
            conn.listeners.append(self._on_event)
            conn.call("Target.setDiscoverTargets", {"discover": True}, cancel=cancel, timeout=SETUP_TIMEOUT)
            infos = conn.call("Target.getTargets", cancel=cancel, timeout=SETUP_TIMEOUT).get("targetInfos") or []
            self.tabs.sync(infos)
        except BaseException:
            conn.close()
            raise
        if self._conn is not None or self.state is not BrowserState.STOPPED:
            self.reconnects += 1
        self._session_id = ""
        self._note(BrowserState.CONNECTED, "connected", f"connected to the remote browser {self.shown_endpoint}")
        return conn

    def download(self, element_id: str, *, cancel: CancellationToken | None = None) -> Download:
        raise IntegrationError(
            "Downloads from a remote browser are saved on its computer: HighhX cannot follow or verify them here."
        )


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if hasattr(os, "WNOHANG"):
        with contextlib.suppress(ChildProcessError, OSError):
            if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our child exited: reap it (not a zombie "alive")
                return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)  # nosec B603 B607 - fixed argv, no shell; well-known system tool resolved from PATH
            return
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    for _ in range(50):
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)  # reap if it is our child
        if not _alive(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
