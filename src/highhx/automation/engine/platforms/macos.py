"""The bridge protocol on macOS.

Two native paths, each where it is the right tool:

* **System Events / Accessibility via JXA** (``osascript``) — applications, the frontmost
  process, typing and keys into the frontmost application, element presses, menus, the
  clipboard. Every script is fixed; user text (typed text, application and element names)
  only ever travels as an ``argv`` argument, never inside the script source.
* **CoreGraphics / Accessibility via ctypes** (:mod:`.quartz`) — the display, window list,
  pointer input, wheel scrolling, the element at a point, window geometry, and input delivered
  to one process in the background (``CGEventPostToPid``).

macOS drops synthetic events silently when Accessibility is not granted, so every input
operation checks the grant first and says so instead of reporting a click that never happened.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from highhx.automation.engine.bridge import EngineError, accessibility_denied
from highhx.automation.engine.platforms import Backend, feature, png_size
from highhx.automation.engine.protocol import KEY_CODES

_DENIED_MARKERS = ("assistive access", "-25211", "not allowed to send keystrokes")

_FRONTMOST = """
function run() {
  const se = Application('System Events');
  const p = se.processes.whose({frontmost: true})[0];
  let title = '';
  try { title = p.windows.length ? (p.windows[0].name() || '') : ''; } catch (e) {}
  return JSON.stringify({app: p.name(), title: title, pid: p.unixId()});
}
"""
_RUNNING = "function run(argv) { return JSON.stringify({running: Application(argv[0]).running()}); }"
_ACTIVATE = "function run(argv) { Application(argv[0]).activate(); return 'ok'; }"
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
_APPS = """
function run() {
  const se = Application('System Events');
  const procs = se.processes.whose({backgroundOnly: false});
  const out = [];
  for (let i = 0; i < procs.length; i++) {
    const p = procs[i];
    const get = f => { try { return p[f](); } catch (e) { return null; } };
    out.push({name: get('name') || '', pid: get('unixId') || 0, bundle_id: get('bundleIdentifier') || '',
              frontmost: get('frontmost') === true, visible: get('visible') !== false});
  }
  return JSON.stringify({apps: out});
}
"""
_PID = """
function run(argv) {
  const procs = Application('System Events').processes.whose({name: argv[0]});
  return JSON.stringify({pid: procs.length ? procs[0].unixId() : 0});
}
"""
_QUIT = "function run(argv) { Application(argv[0]).quit(); return 'ok'; }"
_MENU = """
function run(argv) {
  const path = JSON.parse(argv[1]);
  const procs = Application('System Events').processes.whose({name: argv[0]});
  if (procs.length === 0) return JSON.stringify({error: 'not_running'});
  let items = procs[0].menuBars[0].menuBarItems;
  let item = null;
  for (let level = 0; level < path.length; level++) {
    const names = [];
    item = null;
    for (let i = 0; i < items.length; i++) {
      let n = ''; try { n = items[i].name() || ''; } catch (e) {}
      names.push(n);
      if (n === path[level]) { item = items[i]; break; }
    }
    if (item === null) return JSON.stringify({error: 'not_found', level: level, names: names.filter(n => n)});
    if (level < path.length - 1) { items = item.menus[0].menuItems; }
  }
  let enabled = true; try { enabled = item.enabled(); } catch (e) {}
  if (!enabled) return JSON.stringify({error: 'disabled'});
  item.click();
  return JSON.stringify({ok: true});
}
"""
_CLIPBOARD_READ = """
ObjC.import('AppKit');
function run() {
  const text = $.NSPasteboard.generalPasteboard.stringForType($.NSPasteboardTypeString);
  return JSON.stringify({text: text.isNil() ? null : text.js});
}
"""
_CLIPBOARD_WRITE = """
ObjC.import('AppKit');
function run(argv) {
  const board = $.NSPasteboard.generalPasteboard;
  board.clearContents;
  return JSON.stringify({ok: board.setStringForType($(argv[0]), $.NSPasteboardTypeString)});
}
"""
ANSI_LAYOUT = "asdfhgzxcv\0bqweryt123465=97-80]ou[ip\0lj'k;\\,/nm."
"""The US (ANSI) keyboard, indexed by macOS virtual key code (kVK_ANSI_A = 0 … kVK_ANSI_Period = 47;
code 10 is not on ANSI keyboards and 36 is Return)."""
ANSI_KEYS = {char: code for code, char in enumerate(ANSI_LAYOUT) if char != "\0"}
"""US-layout virtual key codes for keys delivered in the background (the foreground path types
characters through System Events, which follows the active layout)."""
LINES_PER_STEP = 3
FRAME_SETTLE = 4
"""Points a moved window may differ from its request once settled (the window server snaps)."""


class MacBackend(Backend):
    platform = "macos"

    # ------------------------------------------------------------ plumbing
    def _jxa(self, script: str, *args: str, what: str) -> str:
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

    @staticmethod
    def _quartz() -> Any:
        from highhx.automation.engine.platforms import quartz

        return quartz

    def _trusted(self) -> Any:
        """The native layer, once Accessibility is known to be granted (events are dropped otherwise)."""
        quartz = self._quartz()
        if not quartz.accessibility_trusted():
            raise accessibility_denied()
        return quartz

    def _native(self, what: str, call: Any) -> Any:
        quartz = self._quartz()
        try:
            return call()
        except quartz.NativeError as exc:
            raise EngineError("not_found" if exc.code else "failed", f"{what}: {exc}") from None

    def _pid(self, app: str) -> int:
        pid = int(self._json(_PID, app, what=f"Find {app}").get("pid") or 0)
        if not pid:
            raise EngineError("not_found", f"{app} is not running.", hint=f"open {app} first")
        return pid

    # ------------------------------------------------------------ capabilities
    def features(self) -> dict[str, dict[str, Any]]:
        quartz = self._quartz()
        trusted = quartz.accessibility_trusted()
        capture = quartz.screen_capture_allowed()
        grant_ax = "grant Accessibility: System Settings → Privacy & Security → Accessibility"
        grant_capture = "grant Screen Recording: System Settings → Privacy & Security → Screen Recording"

        def needs_ax(what: str) -> dict[str, Any]:
            return feature(trusted, what if trusted else grant_ax)

        return {
            "accessibility": needs_ax("macOS Accessibility (AX API, System Events)"),
            "screenshot": feature(capture, "screencapture" if capture else grant_capture),
            "window_screenshot": feature(capture, "screencapture -l" if capture else grant_capture),
            "pointer": needs_ax("CoreGraphics events"),
            "keyboard": needs_ax("System Events keystrokes; CoreGraphics events in the background"),
            "background_input": needs_ax(
                "CGEventPostToPid — best effort: some applications only accept input while in front"
            ),
            "windows": feature(
                True, "CoreGraphics window list" + ("" if capture else "; titles need Screen Recording")
            ),
            "window_frame": needs_ax("Accessibility window position and size"),
            "applications": feature(True, "System Events processes"),
            "menus": needs_ax("System Events menu bar"),
            "clipboard": feature(True, "NSPasteboard (plain text)"),
            "element_at": needs_ax("AXUIElementCopyElementAtPosition"),
        }

    # ----------------------------------------------------- applications (v1)
    def op_frontmost(self) -> dict[str, Any]:
        data = self._json(_FRONTMOST, what="Find the frontmost application")
        out: dict[str, Any] = {"app": str(data.get("app") or ""), "title": str(data.get("title") or "")}
        pid = int(data.get("pid") or 0)
        if pid:
            out["pid"] = pid
            window = next((w for w in self._quartz().windows() if w["pid"] == pid), None)
            if window is not None:
                out["window"] = window
        return out

    def op_running(self, app: str) -> dict[str, Any]:
        try:
            data = self._json(_RUNNING, app, what=f"Check {app}")
        except EngineError as exc:
            if "-2700" in exc.message or "can't be found" in exc.message or "-1728" in exc.message:
                # LaunchServices does not know the name (not installed, or an application outside the
                # usual folders): whether it runs is whether a process by that name exists
                return {"app": app, "running": bool(self._json(_PID, app, what=f"Find {app}").get("pid"))}
            raise
        return {"app": app, "running": bool(data.get("running"))}

    def op_launch(self, app: str) -> dict[str, Any]:
        code, out, err = self.runner(["open", "-a", app], f"Launch {app}")
        if code != 0:
            raise EngineError("not_found", f"{app} could not be launched: {(err or out).strip()[-200:]}")
        running = self.poll(lambda: self.op_running(app)["running"], 10)
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

        return {"app": app, "frontmost": self.poll(frontmost, 3), "actual": front}

    def op_open_url(self, url: str, app: str | None = None) -> dict[str, Any]:
        argv = ["open", "-a", app, url] if app else ["open", url]
        self.run(argv, f"Open {url}")
        return {"url": url, "app": app or "default browser"}

    def op_quit(self, app: str) -> dict[str, Any]:
        if not self.op_running(app)["running"]:
            raise EngineError("not_found", f"{app} is not running.")
        self._jxa(_QUIT, app, what=f"Quit {app}")
        gone = self.poll(lambda: not self.op_running(app)["running"], 5)
        return {
            "app": app,
            "quit": gone,
            "detail": "" if gone else f"{app} is still running (it may be asking to save)",
        }

    def op_apps(self) -> dict[str, Any]:
        return {"apps": self._json(_APPS, what="List applications").get("apps") or []}

    # --------------------------------------------------------- keyboard (v1)
    def op_type(self, text: str, app: str | None = None) -> dict[str, Any]:
        if app:
            pid = self._pid(app)
            self._native("Type text", lambda: self._trusted().type_text(text, pid=pid))
            return {"characters": len(text), "app": app, "background": True}
        self._jxa(_TYPE, text, what="Type text")
        return {"characters": len(text)}

    def _press(self, key: str, modifiers: list[str], app: str | None) -> None:
        if app:
            code = KEY_CODES.get(key, ANSI_KEYS.get(key))
            if code is None:
                raise EngineError(
                    "unsupported", f"{key!r} cannot be sent in the background (letters, digits and named keys can)."
                )
            pid = self._pid(app)
            self._native(f"Press {key}", lambda: self._trusted().key(code, modifiers, pid=pid))
            return
        using = json.dumps(modifiers)
        if key in KEY_CODES:
            self._jxa(_KEYCODE, str(KEY_CODES[key]), using, what=f"Press {key}")
        else:
            self._jxa(_KEYSTROKE, key, using, what=f"Press {key}")

    def op_key(self, key: str, app: str | None = None) -> dict[str, Any]:
        self._press(key, [], app)
        return {"key": key, **({"app": app, "background": True} if app else {})}

    def op_hotkey(self, modifiers: list[str], key: str, app: str | None = None) -> dict[str, Any]:
        self._press(key, modifiers, app)
        return {"keys": "+".join([*modifiers, key]), **({"app": app, "background": True} if app else {})}

    # --------------------------------------------------------- pointer (v2)
    def op_scroll(self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        lines = amount * LINES_PER_STEP
        dy = {"down": lines, "up": -lines}.get(direction, 0)
        dx = {"right": lines, "left": -lines}.get(direction, 0)
        at = (x, y) if x is not None and y is not None else None
        self._native("Scroll", lambda: self._trusted().scroll(dy, dx, at=at))
        return {"direction": direction, "amount": amount, **({"x": x, "y": y} if at else {})}

    def op_click_at(
        self, x: int, y: int, button: str = "left", count: int = 1, app: str | None = None
    ) -> dict[str, Any]:
        quartz = self._trusted()
        pid = self._pid(app) if app else None
        try:
            element: dict[str, Any] | None = quartz.element_at(x, y)
        except quartz.NativeError:
            element = None  # nothing accessible there (e.g. a canvas): the click still happens where asked
        self._native("Click", lambda: quartz.click(x, y, button=button, count=count, pid=pid))
        out: dict[str, Any] = {"x": x, "y": y, "button": button, "count": count, "element": element}
        if app:
            out |= {"app": app, "background": True}
        return out

    def op_move(self, x: int, y: int) -> dict[str, Any]:
        self._native("Move the pointer", lambda: self._trusted().move(x, y))
        return {"x": x, "y": y}

    def op_cursor(self) -> dict[str, Any]:
        x, y = self._quartz().cursor()
        return {"x": x, "y": y}

    def op_drag(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left", duration_ms: int = 300
    ) -> dict[str, Any]:
        quartz = self._trusted()
        self._native(
            "Drag", lambda: quartz.drag((from_x, from_y), (to_x, to_y), button=button, duration=duration_ms / 1000)
        )
        return {"from": [from_x, from_y], "to": [to_x, to_y], "button": button}

    # ------------------------------------------------------- observation (v2)
    def op_screen(self) -> dict[str, Any]:
        return dict(self._quartz().screen())

    def op_screenshot(self, path: str, window: int | None = None) -> dict[str, Any]:
        if not self._quartz().screen_capture_allowed():
            raise EngineError(
                "screen_recording_denied",
                "macOS has not granted Screen Recording, so a capture would show only the desktop.",
                hint="Allow your terminal in System Settings → Privacy & Security → Screen Recording.",
            )
        target = Path(path)
        self.run(
            ["screencapture", "-x", "-t", "png", *(["-l", str(window)] if window else []), str(target)],
            "Capture the screen",
        )
        if not target.is_file():
            raise EngineError("failed", "The screen capture produced no file.")
        width, height = png_size(target)
        return {
            "path": str(target),
            "width": width,
            "height": height,
            "scale": self.op_screen()["scale"],
            "window": window,
        }

    def op_windows(self, app: str | None = None) -> dict[str, Any]:
        found = self._quartz().windows()
        if app:
            found = [w for w in found if w["app"].lower() == app.lower()]
        return {"windows": found}

    def op_window_frame(self, window: int, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        quartz = self._trusted()
        current = next((w for w in quartz.windows() if w["id"] == window), None)
        if current is None:
            raise EngineError("not_found", f"There is no on-screen window {window}.")
        match = (current["x"], current["y"], current["width"], current["height"])
        if not self._native(
            "Move the window", lambda: quartz.set_window_frame(current["pid"], match, (x, y, width, height))
        ):
            raise EngineError(
                "not_found", f"Window {window} of {current['app']} is not reachable through Accessibility."
            )
        wanted = [x, y, width, height]
        after = current

        def arrived() -> bool:  # the window server updates its list asynchronously
            nonlocal after
            after = next((w for w in quartz.windows() if w["id"] == window), current)
            frame = [after["x"], after["y"], after["width"], after["height"]]
            return all(abs(a - b) <= FRAME_SETTLE for a, b in zip(frame, wanted, strict=True))

        self.poll(arrived, 2)
        return {
            "window": window,
            "app": current["app"],
            "frame": [after["x"], after["y"], after["width"], after["height"]],
        }

    def op_element_at(self, x: int, y: int) -> dict[str, Any]:
        quartz = self._trusted()
        element = dict(self._native("Find the element", lambda: quartz.element_at(x, y)))
        owner = next((w["app"] for w in quartz.windows() if w["pid"] == element["pid"]), "")
        return {**element, "app": owner}

    def op_menu(self, app: str, path: list[str]) -> dict[str, Any]:
        data = self._json(_MENU, app, json.dumps(path), what=f"Choose {' > '.join(path)} in {app}")
        error = data.get("error")
        if error == "not_running":
            raise EngineError("not_found", f"{app} is not running.")
        if error == "not_found":
            level = int(data.get("level") or 0)
            names = ", ".join(str(n) for n in (data.get("names") or [])[:15])
            raise EngineError("not_found", f"{app} has no menu item {path[level]!r} there (it has: {names}).")
        if error == "disabled":
            raise EngineError("refused", f"{' > '.join(path)} is disabled in {app}.")
        return {"app": app, "path": path}

    def op_clipboard_read(self) -> dict[str, Any]:
        text = self._json(_CLIPBOARD_READ, what="Read the clipboard").get("text")
        return {"text": text or "", "has_text": text is not None}

    def op_clipboard_write(self, text: str) -> dict[str, Any]:
        if not self._json(_CLIPBOARD_WRITE, text, what="Write the clipboard").get("ok"):
            raise EngineError("failed", "The clipboard was not changed.")
        return {"characters": len(text)}

    # --------------------------------------------- accessibility tree (v1)
    def _observe(self, app: str | None) -> Any:
        from highhx.computer.desktop import AccessibilityPermissionError, MacAccessibility

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
                {
                    "role": e.role,
                    "name": e.name,
                    "value": "" if e.secret else e.value,
                    "enabled": e.enabled,
                    "focused": e.focused,
                    "secure": e.secret,
                    **({"bounds": list(e.bounds)} if e.bounds else {}),
                }
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
