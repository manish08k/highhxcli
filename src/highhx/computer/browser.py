"""Chrome / Chromium / Edge / Brave automation through the Chrome DevTools Protocol.

The browser is started with a dedicated HighhX profile (never the user's
personal profile) and a DevTools port bound to 127.0.0.1. Observations come from
the DOM with accessible names; password and payment field values never leave
the page. The session is recorded in the user state directory so separate
`highhx computer` invocations reuse the same browser.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import signal
import subprocess  # nosec B404 - subprocess used with fixed argv only
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.computer.websocket import WebSocket, WebSocketClosed, WebSocketTimeout
from highhx.core.errors import IntegrationError, OperationCancelledError, OutcomeUnknownError, ToolNotFoundError
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


class CDPConnection:
    """One DevTools WebSocket session with request/response matching."""

    def __init__(self, ws_url: str, *, timeout: float = 30.0, target_id: str = "") -> None:
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self.target_id = target_id
        self.crashed = False
        """The page's renderer crashed (Inspector.targetCrashed); the page must be reopened."""
        self.dialogs: list[dict[str, Any]] = []
        """JavaScript dialogs HighhX closed so they would not block the page."""
        self._ids = itertools.count(1)
        self.events: list[dict[str, Any]] = []
        self.navigations = 0
        """Navigations requested/started in any frame since this connection opened."""
        self.settled = 0
        """Loads that finished (or same-document navigations) since this connection opened."""
        self.loading_frames: set[str] = set()
        self.main_frame = ""
        """The top-level frame's id (from Page.frameNavigated), once known."""

    @property
    def usable(self) -> bool:
        return not self.ws.closed and not self.crashed

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        cancel: CancellationToken | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.crashed:
            raise PageCrashedError("The page crashed; it has to be reopened.")
        message_id = next(self._ids)
        try:
            self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        except WebSocketClosed:
            # Nothing reached the browser, so sending it again on a new connection is safe.
            raise BrowserDisconnectedError(f"The browser connection was lost before {method} was sent.") from None
        # From here on the browser may have acted on the command: a lost answer is an unknown
        # outcome, never a reason to send it again (only idempotent navigation is re-issued, and
        # only after looking at where the page is — see ChromeBrowser.navigate).
        limit = timeout or self.timeout
        deadline = time.monotonic() + limit
        while True:
            try:
                if time.monotonic() > deadline:  # a steady stream of events must not extend the wait
                    self.ws.close()
                    raise WebSocketTimeout("No answer from the browser in time.")
                raw = self.ws.recv(cancel=cancel, deadline=deadline)
            except WebSocketTimeout:
                raise BrowserTimeoutError(
                    f"The browser did not answer {method} within {limit:.0f}s; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            except WebSocketClosed:
                raise OutcomeUnknownError(
                    f"The browser connection was lost after {method} was sent; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # not a DevTools message; never let it end the session
            if message.get("id") == message_id:
                if "error" in message:
                    raise IntegrationError(f"Browser error in {method}: {message['error'].get('message')}")
                result: dict[str, Any] = message.get("result") or {}
                return result
            if "method" in message:
                self._track(message)
                self.events.append(message)
                del self.events[:-200]
                if self.crashed:
                    raise PageCrashedError(
                        f"The page crashed while {method} was running; it may or may not have run.",
                        hint="HighhX reopens the page on the next action; observe it before deciding again.",
                    )

    def _handle_dialog(self, params: dict[str, Any]) -> None:
        """A JavaScript dialog blocks every script on the page (and so every HighhX command).
        Close it right away: an alert is acknowledged; anything that asks for a decision (confirm,
        prompt, "leave page? unsaved changes will be lost") is cancelled — the answer that never
        discards or confirms anything — and recorded."""
        kind = str(params.get("type") or "")
        accept = kind == "alert"
        self.dialogs.append({"type": kind, "message": str(params.get("message") or "")[:300], "accepted": accept})
        del self.dialogs[:-20]
        # fire-and-forget (its answer is ignored by id); never nested inside call()
        with contextlib.suppress(WebSocketClosed):
            self.ws.send(
                json.dumps(
                    {"id": next(self._ids), "method": "Page.handleJavaScriptDialog", "params": {"accept": accept}}
                )
            )

    def _track(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        frame = str((message.get("params") or {}).get("frameId") or "")
        if method == "Inspector.targetCrashed":
            self.crashed = True
        elif method == "Inspector.detached":
            self.ws.close()  # the target went away or another client took it over
        elif method == "Page.javascriptDialogOpening":
            self._handle_dialog(message.get("params") or {})
        if method in NAVIGATION_STARTED:
            self.navigations += 1
        if method == "Page.frameStartedLoading":
            self.loading_frames.add(frame)
        elif method == "Page.frameStoppedLoading":
            self.loading_frames.discard(frame)
            self.settled += 1
        elif method == "Page.navigatedWithinDocument":
            self.settled += 1
        elif method == "Page.frameDetached":
            # an iframe removed while loading never reports frameStoppedLoading
            self.loading_frames.discard(frame)
        elif method == "Page.frameNavigated":
            info = (message.get("params") or {}).get("frame") or {}
            if not info.get("parentId") and info.get("id"):
                self.main_frame = str(info["id"])

    @property
    def main_frame_loading(self) -> bool:
        """The top-level document is loading (unknown main frame: any frame counts)."""
        if not self.main_frame:
            return bool(self.loading_frames)
        return self.main_frame in self.loading_frames

    def close(self) -> None:
        self.ws.close()


NAVIGATION_STARTED = frozenset(
    {"Page.frameRequestedNavigation", "Page.frameScheduledNavigation", "Page.frameStartedLoading"}
)
CONNECT_ATTEMPTS = 3
"""Connection attempts (with backoff) before a browser action fails with a clear error."""
NAVIGATE_ATTEMPTS = 3
"""Times an idempotent navigation is issued when the connection drops or the page crashes."""
NAVIGATE_TIMEOUT = 45.0
"""Seconds Page.navigate may take to answer (it answers once the response headers arrive)."""
SETUP_TIMEOUT = 5.0
"""Seconds each session set-up command may take; past that the tab has a stalled load, which is stopped."""
NEW_TAB_GRACE = 2.0
"""Seconds a new tab opened by a clicked link gets to show the link's address."""
NAVIGATION_GRACE = 0.5
SUBFRAME_GRACE = 3.0
"""How long to wait for iframes (ads, embeds) once the page itself is complete."""
"""Seconds an action gets to start a navigation (form submit, link) before the page counts as settled."""


class ChromeBrowser:
    """A HighhX-controlled Chromium-family browser (the BrowserAutomationProvider)."""

    name = "browser"

    def __init__(self, state_dir: Path, *, headless: bool | None = None, binary: str | None = None) -> None:
        self.state_dir = state_dir
        self.state_file = state_dir / "browser.json"
        self.profile_dir = state_dir / "browser-profile"
        self.binary = binary or find_browser()
        self.headless = headless if headless is not None else _no_display()
        self._conn: CDPConnection | None = None
        self._mark: tuple[CDPConnection, int, int] | None = None
        self._target_id = ""
        """The tab HighhX works in: reconnections return to it rather than to whichever tab is first."""
        self._tabs_before: set[str] | None = None
        """Tabs that existed before a click, to follow the tab a clicked link opened."""
        self._clicked_href = ""
        """The address of the link the last click activated ('' when it was not a link)."""
        self.reconnects = 0
        self.last_wait_settled = True
        """Whether the last wait for the page ended because it settled (False: it timed out)."""

    # ----------------------------------------------------------------- state
    def capability(self) -> Capability:
        if not self.binary:
            return Capability(self.name, False, "no Chrome, Chromium, Edge or Brave found (set HIGHHX_BROWSER)")
        return Capability(
            self.name, True, f"{Path(self.binary).name} via DevTools ({'headless' if self.headless else 'visible'})"
        )

    def _state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not _alive(int(data.get("pid") or 0)):
            return None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(data['port'])}/json/version", timeout=2) as r:  # nosec B310
                r.read()
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
        self._stop_unresponsive()
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
        with contextlib.suppress(OSError):
            self.state_file.unlink()
        if state is None:
            return self._stop_unresponsive()
        _terminate(int(state["pid"]))
        return True

    def _stop_unresponsive(self) -> bool:
        """A browser HighhX started that is still running but no longer answers DevTools (hung,
        or its port is gone) holds the profile lock, so a new one would exit at once. Stop it —
        only when the process really is HighhX's browser on HighhX's profile (never a reused pid)."""
        try:
            data = json.loads(self.state_file.read_text())
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        if not _alive(pid) or not _uses_profile(pid, self.profile_dir):
            return False
        _terminate(pid)
        with contextlib.suppress(OSError):
            self.state_file.unlink()
        return True

    # ------------------------------------------------------------ connection
    def _devtools(self, port: int, path: str, *, method: str = "GET") -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        with urllib.request.urlopen(request, timeout=5) as r:  # nosec B310 - local DevTools endpoint
            body = r.read()
        return json.loads(body) if body.strip().startswith((b"{", b"[")) else None

    def _pages(self, port: int) -> list[dict[str, Any]]:
        targets = self._devtools(port, "/json/list") or []
        return [
            t
            for t in targets
            if t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")
            and not str(t.get("url") or "").startswith(("devtools://", "chrome-extension://"))
        ]

    def _connection(self, cancel: CancellationToken | None) -> CDPConnection:
        """The live page session — reconnecting (and restarting the browser, and reopening a
        crashed or closed tab) when needed, with a bounded number of attempts."""
        if self._conn is not None and self._conn.usable:
            return self._conn
        replacing = self._conn is not None
        crashed = self._conn is not None and self._conn.crashed
        self.close()
        last: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            if cancel is not None and cancel.cancelled:
                raise OperationCancelledError("Browser operation cancelled.")
            try:
                self._conn = self._open_session(crashed=crashed, cancel=cancel)
                if replacing:
                    self.reconnects += 1
                return self._conn
            except (OSError, ValueError, KeyError, IntegrationError) as exc:
                last = exc
                self.close()
                crashed = False
                if attempt + 1 < CONNECT_ATTEMPTS and self._pause(cancel, 0.25 * (2**attempt)):
                    raise OperationCancelledError("Browser operation cancelled.") from None
        detail = last.message if isinstance(last, IntegrationError) else str(last)
        raise IntegrationError(
            f"Could not connect to the browser after {CONNECT_ATTEMPTS} attempts: {detail}",
            hint="Run `highhx computer browser stop` to reset the HighhX browser, then try again.",
        )

    def _open_session(self, *, crashed: bool, cancel: CancellationToken | None) -> CDPConnection:
        port = int(self.start(cancel=cancel)["port"])
        pages = self._pages(port)
        if crashed and self._target_id:
            # a crashed renderer cannot be trusted again: close its tab and start a fresh one
            with contextlib.suppress(OSError, ValueError):
                self._devtools(port, f"/json/close/{self._target_id}")
            pages = [p for p in pages if p.get("id") != self._target_id]
            self._target_id = ""
        page = next((p for p in pages if self._target_id and p.get("id") == self._target_id), None)
        if page is None and pages:
            page = pages[0]
        if page is None:
            page = self._devtools(port, "/json/new?about:blank", method="PUT")
        try:
            conn = self._attach(page, cancel)
        except BrowserTimeoutError:
            # A navigation that never committed (a server that does not answer, a client that gave
            # up on it) holds every command of a new session — except Page.stopLoading, which the
            # browser answers itself. Abandon that load and attach again; opening is idempotent.
            stopper = CDPConnection(str(page["webSocketDebuggerUrl"]), target_id=str(page.get("id") or ""))
            try:
                stopper.call("Page.stopLoading", cancel=cancel, timeout=SETUP_TIMEOUT)
            finally:
                stopper.close()
            conn = self._attach(page, cancel)
        self._target_id = conn.target_id
        return conn

    def _attach(self, page: dict[str, Any], cancel: CancellationToken | None) -> CDPConnection:
        """A new DevTools session on ``page`` with the domains and settings HighhX relies on."""
        conn = CDPConnection(str(page["webSocketDebuggerUrl"]), target_id=str(page.get("id") or ""))
        try:
            conn.call("Page.enable", cancel=cancel, timeout=SETUP_TIMEOUT)
            tree = conn.call("Page.getFrameTree", cancel=cancel, timeout=SETUP_TIMEOUT)
            conn.main_frame = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
            conn.call("Runtime.enable", cancel=cancel, timeout=SETUP_TIMEOUT)
            conn.call("Inspector.enable", cancel=cancel, timeout=SETUP_TIMEOUT)
            with contextlib.suppress(IntegrationError):
                # keys reach the page even when the window is behind the terminal or its address
                # bar has keyboard focus (otherwise Enter can silently go nowhere)
                conn.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, cancel=cancel, timeout=SETUP_TIMEOUT)
            with contextlib.suppress(IntegrationError, OSError):  # else downloads keep the browser default
                self.downloads_dir.mkdir(parents=True, exist_ok=True)
                conn.call(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(self.downloads_dir)},
                    cancel=cancel,
                    timeout=SETUP_TIMEOUT,
                )
        except BaseException:
            conn.close()
            raise
        return conn

    @property
    def downloads_dir(self) -> Path:
        return self.state_dir / "downloads"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _send(
        self, method: str, params: dict[str, Any] | None = None, *, cancel: CancellationToken | None = None
    ) -> dict[str, Any]:
        """One DevTools command on the live session. A command that never reached the browser is
        sent once more on a fresh connection; one that may have run is never repeated here."""
        try:
            return self._connection(cancel).call(method, params, cancel=cancel)
        except BrowserDisconnectedError:
            self.close()
            return self._connection(cancel).call(method, params, cancel=cancel)

    def _before_action(self, cancel: CancellationToken | None, *, follow_tabs: bool = False) -> CDPConnection:
        """Remember the navigation count so ``wait_ready`` can tell whether the action started one
        (and, for clicks, which tabs exist so a tab the clicked link opens is followed)."""
        conn = self._connection(cancel)
        self._mark = (conn, conn.navigations, conn.settled)
        self._tabs_before, self._clicked_href = None, ""
        if follow_tabs:
            with contextlib.suppress(OSError, ValueError, KeyError, IntegrationError):
                self._tabs_before = {str(p.get("id")) for p in self._pages(int(self.start(cancel=cancel)["port"]))}
        return conn

    def _eval(self, expression: str, cancel: CancellationToken | None, *, user_gesture: bool = False) -> Any:
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if user_gesture:
            params["userGesture"] = True  # like a person's click: a target=_blank link may open its tab
        result = self._send("Runtime.evaluate", params, cancel=cancel)
        if result.get("exceptionDetails"):
            raise IntegrationError(f"Page script failed: {result['exceptionDetails'].get('text')}")
        return (result.get("result") or {}).get("value")

    def _abandon_load(self, cancel: CancellationToken | None) -> None:
        """Stop a load that will not finish, so the tab answers the next command at once."""
        with contextlib.suppress(IntegrationError, OSError, ValueError, KeyError):
            self._send("Page.stopLoading", cancel=cancel)

    def current_url(self, *, cancel: CancellationToken | None = None) -> str:
        return str(self._eval("location.href", cancel) or "")

    # ----------------------------------------------------------------- actions
    def navigate(self, url: str, *, cancel: CancellationToken | None = None) -> None:
        """Open ``url`` and wait for it to load.

        Opening a URL is idempotent, so unlike a click it may be issued again — but only after
        reconnecting and looking at where the page is: if it already got there, it is not
        requested again. Attempts are bounded; every failure says what happened."""
        problems: list[str] = []
        for attempt in range(1, NAVIGATE_ATTEMPTS + 1):
            try:
                if attempt > 1 and _same_document(url, self.current_url(cancel=cancel)):
                    self.wait_ready(cancel=cancel)  # it got there before the connection dropped
                    return
                conn = self._before_action(cancel)
                result = conn.call("Page.navigate", {"url": url}, cancel=cancel, timeout=NAVIGATE_TIMEOUT)
                error = str(result.get("errorText") or "")
                if error and error != "net::ERR_ABORTED":  # aborted: replaced by a redirect or a download
                    raise IntegrationError(f"Could not open {url}: {error}", hint=_network_hint(error))
                self.wait_ready(cancel=cancel)
                return
            except BrowserTimeoutError:
                # the connection is fine; the site is not answering. Retrying would only wait again.
                self._abandon_load(cancel)
                raise IntegrationError(
                    f"Could not open {url}: the site did not respond within {NAVIGATE_TIMEOUT:.0f}s.",
                    hint="The site may be down or very slow; HighhX stopped loading it. Try again later.",
                ) from None
            except (BrowserDisconnectedError, OutcomeUnknownError) as exc:
                # the connection dropped or the page crashed mid-navigation; the next attempt
                # reconnects (reopening a crashed tab) and looks before asking again
                problems.append(f"attempt {attempt}: {exc.message}")
        raise IntegrationError(
            f"Could not open {url}: the browser connection failed {NAVIGATE_ATTEMPTS} times.",
            hint="HighhX reconnected and checked the page before each retry. Run `highhx computer browser stop` "
            "to reset the browser if this keeps happening.",
            details=problems,
        )

    def wait_ready(self, *, timeout: float = 30.0, cancel: CancellationToken | None = None) -> None:
        """Wait until the page settled after the last action.

        ``document.readyState`` alone is not enough: right after a click or Enter the *old*
        document is still "complete" for a moment before the navigation it triggered starts.
        So first give the action a short grace period to start a navigation, then wait until
        no frame is loading and the (new) document is complete.

        A slow page is not an error: after ``timeout`` the wait ends and
        :attr:`last_wait_settled` is False, so callers observe whatever has loaded. A crashed
        page is an error (:class:`PageCrashedError`); a dropped connection is reconnected.
        """
        deadline = time.monotonic() + timeout
        mark, self._mark = self._mark, None
        tabs_before, self._tabs_before = self._tabs_before, None
        navigating: tuple[CDPConnection, int] | None = None
        if mark is not None:
            conn, before, settled = mark
            grace = time.monotonic() + NAVIGATION_GRACE
            while conn is self._conn and conn.navigations == before and time.monotonic() < grace:
                try:
                    self._eval("0", cancel)  # also drains pending navigation events
                except PageCrashedError:
                    raise
                except (IntegrationError, WebSocketClosed):
                    break
                if self._pause(cancel):
                    raise OperationCancelledError("Browser operation cancelled.")
            if conn.navigations != before:
                navigating = (conn, settled)  # the page itself moved on: stay (a popup beside it is an ad)
            elif tabs_before is not None and self._clicked_href:
                self._follow_new_tab(tabs_before, self._clicked_href, cancel)  # then wait for its document
        page_complete_at: float | None = None
        self.last_wait_settled = False
        while time.monotonic() < deadline:
            try:
                conn = self._connection(cancel)
                complete = self._eval("document.readyState", cancel) == "complete" and not conn.main_frame_loading
                if navigating is not None and navigating[0] is conn:
                    complete = complete and conn.settled > navigating[1]  # the navigation itself finished
                if complete:
                    page_complete_at = page_complete_at or time.monotonic()
                    # iframes (ads, embeds) get a short grace, not the whole timeout
                    if not conn.loading_frames or time.monotonic() - page_complete_at >= SUBFRAME_GRACE:
                        self.last_wait_settled = True
                        return
                else:
                    page_complete_at = None
            except PageCrashedError:
                raise
            except (IntegrationError, WebSocketClosed):
                pass
            if self._pause(cancel):
                raise OperationCancelledError("Browser operation cancelled.")

    def _follow_new_tab(self, before: set[str], href: str, cancel: CancellationToken | None) -> bool:
        """A clicked link that opened its address in a new tab (target=_blank) continues there.
        Any other new tab — a script's window.open, an ad — is not followed: HighhX stays on the
        page the person was working in."""
        deadline = time.monotonic() + NEW_TAB_GRACE
        while True:
            try:
                pages = self._pages(int(self.start(cancel=cancel)["port"]))
            except (OSError, ValueError, KeyError, IntegrationError):
                return False
            new = [p for p in pages if str(p.get("id")) not in before]
            match = next((p for p in new if _same_document(href, str(p.get("url") or ""))), None)
            if match is not None:
                self.close()
                self._target_id = str(match.get("id") or "")
                return True
            # a new tab reports about:blank until its request starts; anything else is not ours
            if not any(str(p.get("url") or "") in ("", "about:blank") for p in new):
                return False
            if time.monotonic() >= deadline or self._pause(cancel, 0.1):
                return False

    @staticmethod
    def _pause(cancel: CancellationToken | None, seconds: float = 0.05) -> bool:
        if cancel is not None:
            return cancel.wait(seconds)
        time.sleep(seconds)
        return False

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
        return Observation(
            provider=self.name,
            application=browser,
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            elements=elements,
            text=str(data.get("text") or ""),
            captured_at=time.time(),
        )

    def _act(self, element_id: str, action: str, arg: str = "", cancel: CancellationToken | None = None) -> None:
        self._before_action(cancel, follow_tabs=action == "click")
        result = self._eval(
            f"({ACT_JS})({json.dumps(element_id)}, {json.dumps(action)}, {json.dumps(arg)})",
            cancel,
            user_gesture=action == "click",
        )
        if not result or not result.get("found"):
            raise ElementNotFoundError(f"Element {element_id} is no longer on the page.")
        if result.get("error"):
            raise IntegrationError(f"Could not {action} {element_id}: {result['error']}")
        if self._tabs_before is not None:
            self._clicked_href = str(result.get("href") or "")

    def evaluate(self, expression: str, *, cancel: CancellationToken | None = None) -> Any:
        """Evaluate a fixed HighhX script in the page (never text from a model or a page)."""
        return self._eval(expression, cancel)

    def screenshot(self, *, cancel: CancellationToken | None = None) -> bytes:
        """The current page as PNG bytes (read-only; nothing on the page changes)."""
        import base64

        data = self._send("Page.captureScreenshot", {"format": "png"}, cancel=cancel)
        return base64.b64decode(str(data.get("data") or ""))

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "click", cancel=cancel)

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "clear", cancel=cancel)
        self._send("Input.insertText", {"text": text}, cancel=cancel)

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        if key not in KEY_CODES:
            raise IntegrationError(f"Unsupported key {key!r}")
        name, code, vk, text = KEY_CODES[key]
        self._before_action(cancel)
        down: dict[str, Any] = {"type": "keyDown", "key": name, "code": code, "windowsVirtualKeyCode": vk}
        if text:
            down["text"] = text
        self._send("Input.dispatchKeyEvent", down, cancel=cancel)
        self._send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": vk},
            cancel=cancel,
        )

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        delta = {"down": 600, "up": -600}.get(direction)
        if delta is None:
            raise IntegrationError(f"Unsupported scroll direction {direction!r}")
        self._eval(f"window.scrollBy(0, {delta})", cancel)

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "select", option, cancel=cancel)


class ElementNotFoundError(IntegrationError):
    """The element from the last observation is gone (the UI changed)."""


class BrowserDisconnectedError(IntegrationError):
    """The connection was gone before a command was sent: nothing happened, so it is safe to
    send the command again on a new connection."""


class BrowserTimeoutError(OutcomeUnknownError):
    """A command got no answer in time (the connection is closed so a late answer is never
    mistaken for another command's); it may or may not have run."""


class PageCrashedError(OutcomeUnknownError):
    """The page's renderer crashed; whatever was running may or may not have taken effect."""


def _no_display() -> bool:
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return bool(os.environ.get("HIGHHX_HEADLESS"))


def _same_document(requested: str, actual: str) -> bool:
    """The page is already at ``requested`` (ignoring a trailing slash and the fragment)."""

    def norm(url: str) -> str:
        return url.split("#", 1)[0].rstrip("/")

    return bool(actual) and norm(requested) == norm(actual)


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


def _alive(pid: int) -> bool:
    if pid <= 0:
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
