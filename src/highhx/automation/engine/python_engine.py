"""The built-in automation engine: the bridge protocol on macOS through System Events and the
Accessibility API (JavaScript for Automation via ``osascript``).

Every process it starts is a fixed argv run through the ``runner`` HighhX gives it (the
command engine: logged, policy-checked, cancellable). User text — typed text, application
and element names — only ever travels as an ``argv`` argument to a fixed script, never
inside the script source.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from highhx.automation.engine.bridge import EngineError, Runner, accessibility_denied
from highhx.automation.engine.protocol import KEY_CODES, PROTOCOL_VERSION
from highhx.execution.cancellation import CancellationToken

_DENIED_MARKERS = ("assistive access", "-25211", "not allowed to send keystrokes")

_FRONTMOST = """
function run() {
  const se = Application('System Events');
  const p = se.processes.whose({frontmost: true})[0];
  let title = '';
  try { title = p.windows.length ? (p.windows[0].name() || '') : ''; } catch (e) {}
  return JSON.stringify({app: p.name(), title: title});
}
"""
_RUNNING = "function run(argv) { return JSON.stringify({running: Application(argv[0]).running()}); }"
_ACTIVATE = "function run(argv) { Application(argv[0]).activate(); return 'ok'; }"
_TRUSTED = "function run() { return JSON.stringify({trusted: Application('System Events').uiElementsEnabled()}); }"
_TYPE = "function run(argv) { Application('System Events').keystroke(argv[0]); return 'ok'; }"
_KEYCODE = """
function run(argv) {
  const using = JSON.parse(argv[1]).map(m => m + ' down');
  Application('System Events').keyCode(Number(argv[0]), {using: using});
  return 'ok';
}
"""
_KEYSTROKE = """
function run(argv) {
  const using = JSON.parse(argv[1]).map(m => m + ' down');
  Application('System Events').keystroke(argv[0], {using: using});
  return 'ok';
}
"""
_WINDOWS = """
function run(argv) {
  const se = Application('System Events');
  const procs = se.processes.whose({name: argv[0]});
  if (procs.length === 0) return JSON.stringify({running: false, windows: []});
  const names = [];
  const ws = procs[0].windows;
  for (let i = 0; i < ws.length; i++) { try { names.push(ws[i].name() || ''); } catch (e) { names.push(''); } }
  return JSON.stringify({running: true, windows: names});
}
"""
_SCROLL_KEYS = {"down": "pagedown", "up": "pageup", "left": "left", "right": "right"}


class PythonEngine:
    name = "python"

    def __init__(self, runner: Runner, *, cancel: CancellationToken | None = None) -> None:
        self.runner = runner
        self.cancel = cancel

    # ------------------------------------------------------------ plumbing
    def _macos(self, op: str) -> None:
        if sys.platform != "darwin":
            raise EngineError(
                "unsupported_platform",
                f"{op}: desktop automation needs macOS here; browser actions work on every platform.",
            )

    def _jxa(self, script: str, *args: str, what: str) -> str:
        self._macos(what)
        code, out, err = self.runner(["osascript", "-l", "JavaScript", "-e", script, *args], what)
        if code != 0:
            detail = (err or out).strip()
            if any(marker in detail for marker in _DENIED_MARKERS):
                raise accessibility_denied(detail[-160:])
            raise EngineError("failed", f"{what} failed: {detail[-300:] or f'exit code {code}'}")
        return out.strip()

    def _json(self, script: str, *args: str, what: str) -> dict[str, Any]:
        raw = self._jxa(script, *args, what=what)
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            raise EngineError("failed", f"{what}: unexpected answer {raw[:120]!r}") from None
        return data if isinstance(data, dict) else {}

    def _sleep(self, seconds: float) -> None:
        if self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def _poll(self, check: Any, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if check():
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep(0.25)

    # ------------------------------------------------------------ operations
    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise EngineError("unknown_op", f"the Python engine does not implement {op!r}")
        result: dict[str, Any] = handler(**args)
        return result

    def close(self) -> None:
        return None

    def op_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"engine": self.name, "protocol": PROTOCOL_VERSION, "platform": sys.platform}
        if sys.platform != "darwin":
            return {**info, "ok": False, "accessibility": False, "detail": "desktop automation needs macOS"}
        try:
            trusted = bool(self._json(_TRUSTED, what="Check Accessibility").get("trusted"))
        except EngineError as exc:
            return {**info, "ok": False, "accessibility": False, "detail": exc.message}
        detail = "System Events and Accessibility via osascript"
        if not trusted:
            detail = "Accessibility is not granted: System Settings → Privacy & Security → Accessibility"
        return {**info, "ok": trusted, "accessibility": trusted, "detail": detail}

    def op_frontmost(self) -> dict[str, Any]:
        data = self._json(_FRONTMOST, what="Find the frontmost application")
        return {"app": str(data.get("app") or ""), "title": str(data.get("title") or "")}

    def op_running(self, app: str) -> dict[str, Any]:
        try:
            data = self._json(_RUNNING, app, what=f"Check {app}")
        except EngineError as exc:
            if "-2700" in exc.message or "can't be found" in exc.message or "-1728" in exc.message:
                raise EngineError("not_found", f"{app} is not installed on this Mac.") from None
            raise
        return {"app": app, "running": bool(data.get("running"))}

    def op_launch(self, app: str) -> dict[str, Any]:
        self._macos("launch")
        code, out, err = self.runner(["open", "-a", app], f"Launch {app}")
        if code != 0:
            raise EngineError("not_found", f"{app} could not be launched: {(err or out).strip()[-200:]}")
        running = self._poll(lambda: self.op_running(app)["running"], 10)
        return {"app": app, "running": running}

    def op_focus(self, app: str) -> dict[str, Any]:
        if not self.op_running(app)["running"]:
            raise EngineError("not_found", f"{app} is not running.", hint=f"open {app} first")
        self._jxa(_ACTIVATE, app, what=f"Switch to {app}")
        front = ""

        def frontmost() -> bool:
            nonlocal front
            front = self.op_frontmost()["app"]
            return front.lower() == app.lower() or app.lower() in front.lower()

        return {"app": app, "frontmost": self._poll(frontmost, 3), "actual": front}

    def op_open_url(self, url: str, app: str | None = None) -> dict[str, Any]:
        if sys.platform == "darwin":
            argv = ["open", "-a", app, url] if app else ["open", url]
        elif sys.platform.startswith("win"):
            argv = ["cmd", "/c", "start", "", *([app] if app else []), url]
        else:
            argv = [app, url] if app else ["xdg-open", url]
        code, out, err = self.runner(argv, f"Open {url}")
        if code != 0:
            raise EngineError("failed", f"Opening {url} failed: {(err or out).strip()[-200:]}")
        return {"url": url, "app": app or "default browser"}

    def op_type(self, text: str) -> dict[str, Any]:
        self._jxa(_TYPE, text, what="Type text")
        return {"characters": len(text)}

    def _press(self, key: str, modifiers: list[str]) -> None:
        using = json.dumps(modifiers)
        if key in KEY_CODES:
            self._jxa(_KEYCODE, str(KEY_CODES[key]), using, what=f"Press {key}")
        else:
            self._jxa(_KEYSTROKE, key, using, what=f"Press {key}")

    def op_key(self, key: str) -> dict[str, Any]:
        self._press(key, [])
        return {"key": key}

    def op_hotkey(self, modifiers: list[str], key: str) -> dict[str, Any]:
        self._press(key, modifiers)
        return {"keys": "+".join([*modifiers, key])}

    def op_scroll(self, direction: str, amount: int = 1) -> dict[str, Any]:
        for _ in range(amount):
            self._press(_SCROLL_KEYS[direction], [])
        return {"direction": direction, "amount": amount}

    def op_wait(self, ms: int) -> dict[str, Any]:
        self._sleep(ms / 1000)
        return {"ms": ms}

    def _observe(self, app: str | None) -> Any:
        from highhx.computer.desktop import AccessibilityPermissionError, MacAccessibility

        self._macos("inspect")
        provider = MacAccessibility(app)
        try:
            return provider, provider.observe(cancel=self.cancel)
        except AccessibilityPermissionError:
            raise accessibility_denied() from None

    def op_inspect(self, app: str | None = None, limit: int = 100) -> dict[str, Any]:
        _provider, observation = self._observe(app)
        return {
            "app": observation.application,
            "title": observation.title,
            "elements": [
                {"role": e.role, "name": e.name, "value": e.value, "enabled": e.enabled, "focused": e.focused}
                for e in observation.elements[:limit]
            ],
        }

    def _find(self, observation: Any, name: str, role: str | None) -> Any:
        wanted = name.lower()
        candidates = [e for e in observation.elements if role in (None, "any") or e.role == role]
        exact = [e for e in candidates if (e.name or "").lower() == wanted]
        if exact:
            return exact[0]
        partial = [e for e in candidates if wanted in (e.name or "").lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = ", ".join(repr(e.name) for e in partial[:5])
            raise EngineError("not_found", f"{name!r} matches several elements ({names}); use the exact name.")
        return None

    def op_click(self, name: str, role: str | None = None, app: str | None = None) -> dict[str, Any]:
        provider, observation = self._observe(app)
        element = self._find(observation, name, role)
        if element is None:
            raise EngineError("not_found", f"No {role or 'element'} named {name!r} in {observation.application}.")
        provider.click(element.id, cancel=self.cancel)
        return {"app": observation.application, "role": element.role, "name": element.name}

    def op_verify(
        self, check: str, app: str | None = None, name: str | None = None, role: str | None = None
    ) -> dict[str, Any]:
        if check == "frontmost":
            front = self.op_frontmost()["app"]
            ok = bool(app) and (front.lower() == str(app).lower() or str(app).lower() in front.lower())
            return {"ok": ok, "detail": f"{front} is frontmost"}
        if check == "running":
            if not app:
                raise EngineError("invalid_request", "verify running needs app")
            ok = self.op_running(app)["running"]
            return {"ok": ok, "detail": f"{app} is {'running' if ok else 'not running'}"}
        if check == "window":
            if not app:
                raise EngineError("invalid_request", "verify window needs app")
            data = self._json(_WINDOWS, app, what=f"List {app} windows")
            windows = [str(w) for w in data.get("windows") or []]
            ok = bool(windows) and (not name or any(name.lower() in w.lower() for w in windows))
            return {"ok": ok, "detail": f"{len(windows)} window(s)", "windows": windows}
        if not name:
            raise EngineError("invalid_request", "verify element needs name")
        _provider, observation = self._observe(app)
        found = self._find(observation, name, role)
        return {"ok": found is not None, "detail": f"{name!r} {'found' if found else 'not found'}"}
