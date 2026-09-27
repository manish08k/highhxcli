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
  if (action === 'click') { e.focus({preventScroll: true}); e.click(); return {found: true}; }
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

    def __init__(self, ws_url: str, *, timeout: float = 30.0) -> None:
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.events: list[dict[str, Any]] = []
        self.navigations = 0
        """Navigations requested/started in any frame since this connection opened."""
        self.settled = 0
        """Loads that finished (or same-document navigations) since this connection opened."""
        self.loading_frames: set[str] = set()
        self.main_frame = ""
        """The top-level frame's id (from Page.frameNavigated), once known."""

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, cancel: CancellationToken | None = None
    ) -> dict[str, Any]:
        message_id = next(self._ids)
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        # From here on the browser may have acted on the command: a lost answer is an unknown
        # outcome, never a reason to send it again. (Reconnection happens lazily on the *next*
        # command; no command is ever re-sent.)
        deadline = time.monotonic() + self.timeout
        while True:
            if time.monotonic() > deadline:
                self.ws.close()
                raise OutcomeUnknownError(
                    f"The browser did not answer {method} within {self.timeout:.0f}s; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                )
            try:
                raw = self.ws.recv(cancel=cancel, deadline=deadline)
            except WebSocketTimeout:
                raise OutcomeUnknownError(
                    f"The browser did not answer {method} within {self.timeout:.0f}s; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            except WebSocketClosed:
                raise OutcomeUnknownError(
                    f"The browser connection was lost after {method} was sent; it may or may not have run.",
                    hint="Observe the page to see what happened; it will not be repeated automatically.",
                ) from None
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise IntegrationError(f"Browser error in {method}: {message['error'].get('message')}")
                result: dict[str, Any] = message.get("result") or {}
                return result
            if "method" in message:
                self._track(message)
                self.events.append(message)
                del self.events[:-200]

    def _track(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        frame = str((message.get("params") or {}).get("frameId") or "")
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
                raise IntegrationError(f"The browser exited during start-up (code {process.returncode}).")
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
        with contextlib.suppress(OSError):
            self.state_file.unlink()
        if state is None:
            return False
        _terminate(int(state["pid"]))
        return True

    # ------------------------------------------------------------ connection
    def _connection(self, cancel: CancellationToken | None) -> CDPConnection:
        if self._conn is not None and not self._conn.ws.closed:
            return self._conn
        state = self.start(cancel=cancel)
        port = int(state["port"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:  # nosec B310 - local
            targets = json.loads(r.read())
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
            with urllib.request.urlopen(request, timeout=5) as r:  # nosec B310 - local
                pages = [json.loads(r.read())]
        self._conn = CDPConnection(pages[0]["webSocketDebuggerUrl"])
        self._conn.call("Page.enable", cancel=cancel)
        tree = self._conn.call("Page.getFrameTree", cancel=cancel)
        self._conn.main_frame = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
        self._conn.call("Runtime.enable", cancel=cancel)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _before_action(self, cancel: CancellationToken | None) -> CDPConnection:
        """Remember the navigation count so ``wait_ready`` can tell whether the action started one."""
        conn = self._connection(cancel)
        self._mark = (conn, conn.navigations, conn.settled)
        return conn

    def _eval(self, expression: str, cancel: CancellationToken | None) -> Any:
        result = self._connection(cancel).call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, cancel=cancel
        )
        if result.get("exceptionDetails"):
            raise IntegrationError(f"Page script failed: {result['exceptionDetails'].get('text')}")
        return (result.get("result") or {}).get("value")

    # ----------------------------------------------------------------- actions
    def navigate(self, url: str, *, cancel: CancellationToken | None = None) -> None:
        conn = self._before_action(cancel)
        result = conn.call("Page.navigate", {"url": url}, cancel=cancel)
        if result.get("errorText"):
            raise IntegrationError(f"Navigation to {url} failed: {result['errorText']}")
        self.wait_ready(cancel=cancel)

    def wait_ready(self, *, timeout: float = 30.0, cancel: CancellationToken | None = None) -> None:
        """Wait until the page settled after the last action.

        ``document.readyState`` alone is not enough: right after a click or Enter the *old*
        document is still "complete" for a moment before the navigation it triggered starts.
        So first give the action a short grace period to start a navigation, then wait until
        no frame is loading and the (new) document is complete.
        """
        deadline = time.monotonic() + timeout
        mark, self._mark = self._mark, None
        navigating: tuple[CDPConnection, int] | None = None
        if mark is not None:
            conn, before, settled = mark
            grace = time.monotonic() + NAVIGATION_GRACE
            while conn is self._conn and conn.navigations == before and time.monotonic() < grace:
                try:
                    self._eval("0", cancel)  # also drains pending navigation events
                except (IntegrationError, WebSocketClosed):
                    break
                if self._pause(cancel):
                    raise OperationCancelledError("Browser operation cancelled.")
            if conn.navigations != before:
                navigating = (conn, settled)
        page_complete_at: float | None = None
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
                        return
                else:
                    page_complete_at = None
            except (IntegrationError, WebSocketClosed):
                pass
            if self._pause(cancel):
                raise OperationCancelledError("Browser operation cancelled.")

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
        self._before_action(cancel)
        result = self._eval(f"({ACT_JS})({json.dumps(element_id)}, {json.dumps(action)}, {json.dumps(arg)})", cancel)
        if not result or not result.get("found"):
            raise ElementNotFoundError(f"Element {element_id} is no longer on the page.")
        if result.get("error"):
            raise IntegrationError(f"Could not {action} {element_id}: {result['error']}")

    def evaluate(self, expression: str, *, cancel: CancellationToken | None = None) -> Any:
        """Evaluate a fixed HighhX script in the page (never text from a model or a page)."""
        return self._eval(expression, cancel)

    def screenshot(self, *, cancel: CancellationToken | None = None) -> bytes:
        """The current page as PNG bytes (read-only; nothing on the page changes)."""
        import base64

        data = self._connection(cancel).call("Page.captureScreenshot", {"format": "png"}, cancel=cancel)
        return base64.b64decode(str(data.get("data") or ""))

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "click", cancel=cancel)

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self._act(element_id, "clear", cancel=cancel)
        self._connection(cancel).call("Input.insertText", {"text": text}, cancel=cancel)

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        if key not in KEY_CODES:
            raise IntegrationError(f"Unsupported key {key!r}")
        name, code, vk, text = KEY_CODES[key]
        conn = self._before_action(cancel)
        down: dict[str, Any] = {"type": "keyDown", "key": name, "code": code, "windowsVirtualKeyCode": vk}
        if text:
            down["text"] = text
        conn.call("Input.dispatchKeyEvent", down, cancel=cancel)
        conn.call(
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


def _no_display() -> bool:
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return bool(os.environ.get("HIGHHX_HEADLESS"))


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
