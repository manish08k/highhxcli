"""The bridge protocol on Linux.

    X11          xdotool — pointer, keyboard, windows (XTest); ``--window`` delivery (XSendEvent)
    AT-SPI       the accessibility tree, element presses, the element at a point, menus
                 (PyGObject's ``gi.repository.Atspi`` — used when installed, reported when not)
    capture      grim (Wayland) · ImageMagick ``import`` · scrot · gnome-screenshot
    clipboard    wl-paste / wl-copy (Wayland) · xclip (X11)
    windows      wmctrl, when installed, closes windows politely (``quit``)

Wayland does not let one client send input to others without the RemoteDesktop portal, which
HighhX does not implement: on a Wayland session pointer and keyboard operations say so rather
than acting on only the XWayland windows. Every process started is a fixed argv through the
command engine; typed text travels as an argument (``xdotool type -- TEXT``).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.platforms import Backend, feature, png_size

XDOTOOL_KEYS = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "space": "space",
    "delete": "BackSpace",
    "backspace": "BackSpace",
    "escape": "Escape",
    "esc": "Escape",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "pageup": "Prior",
    "pagedown": "Next",
    "home": "Home",
    "end": "End",
}
"""The protocol's named keys as X keysyms (``delete`` is the backspace key, as in the protocol)."""
XDOTOOL_MODIFIERS = {"command": "super", "control": "ctrl", "option": "alt", "shift": "shift"}
BUTTON_NUMBERS = {"left": "1", "middle": "2", "right": "3"}
WHEEL = {"up": "4", "down": "5", "left": "6", "right": "7"}
ATSPI_ROLES = {
    "push button": "button",
    "toggle button": "button",
    "link": "link",
    "text": "textbox",
    "entry": "textbox",
    "password text": "textbox",
    "check box": "checkbox",
    "radio button": "radio",
    "combo box": "combobox",
    "menu item": "menuitem",
    "check menu item": "menuitem",
    "radio menu item": "menuitem",
    "menu": "menu",
    "page tab": "tab",
    "slider": "slider",
    "label": "text",
    "heading": "heading",
    "image": "image",
    "icon": "image",
    "frame": "window",
    "window": "window",
    "list item": "option",
}
MAX_WINDOWS = 60


def keysym(key: str) -> str:
    return XDOTOOL_KEYS.get(key, key)


def combo(modifiers: list[str], key: str) -> str:
    return "+".join([*(XDOTOOL_MODIFIERS[m] for m in modifiers), keysym(key)])


def shell_values(text: str) -> dict[str, str]:
    """``xdotool … --shell`` output (``X=10`` lines) as a mapping."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def process_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def running_pids(app: str) -> list[int]:
    """Processes whose name is ``app`` (any case), from /proc — no subprocess needed."""
    wanted = app.lower()
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        if process_name(int(entry.name)).lower() == wanted:
            found.append(int(entry.name))
    return sorted(found)


def atspi() -> Any:
    """``gi.repository.Atspi`` when PyGObject and AT-SPI are installed, else None."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        return None
    return Atspi


class LinuxBackend(Backend):
    platform = "linux"

    # ------------------------------------------------------------ environment
    @staticmethod
    def session() -> str:
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return "wayland"
        return "x11" if os.environ.get("DISPLAY") else "none"

    def _xdotool(self, what: str) -> str:
        session = self.session()
        if session == "wayland":
            raise EngineError(
                "unsupported_platform",
                f"{what}: Wayland does not allow input into other applications without the RemoteDesktop "
                "portal, which HighhX does not implement.",
                hint="Log in to an X11 session to use desktop input, or use browser automation.",
            )
        if session == "none":
            raise EngineError("unsupported_platform", f"{what}: there is no graphical session (DISPLAY is not set).")
        tool = shutil.which("xdotool")
        if tool is None:
            raise EngineError(
                "unsupported_platform", f"{what} needs xdotool.", hint="Install it, e.g. `sudo apt install xdotool`."
            )
        return tool

    def xdo(self, *args: str, what: str) -> str:
        return self.run([self._xdotool(what), *args], what)

    def _tree(self, what: str) -> Any:
        found = atspi()
        if found is None:
            raise EngineError(
                "unsupported_platform",
                f"{what} needs the AT-SPI accessibility bindings.",
                hint="Install PyGObject with AT-SPI, e.g. `sudo apt install python3-gi gir1.2-atspi-2.0`.",
            )
        return found

    def _capture_tool(self) -> str | None:
        order = ("grim",) if self.session() == "wayland" else ("import", "scrot", "gnome-screenshot")
        return next((t for t in order if shutil.which(t)), None)

    def features(self) -> dict[str, dict[str, Any]]:
        session = self.session()
        x11 = session == "x11"
        xdotool = shutil.which("xdotool") is not None
        tree = atspi() is not None
        input_ok = x11 and xdotool
        if session == "wayland":
            why_input = "Wayland needs the RemoteDesktop portal (not implemented)"
        elif not x11:
            why_input = "no graphical session"
        else:
            why_input = "xdotool" if xdotool else "install xdotool"
        capture = self._capture_tool()
        clip = shutil.which("wl-paste") if session == "wayland" else shutil.which("xclip")
        atspi_detail = "AT-SPI (PyGObject)" if tree else "install python3-gi and gir1.2-atspi-2.0"
        return {
            "accessibility": feature(tree, atspi_detail),
            "screenshot": feature(capture is not None, capture or "install grim, ImageMagick or scrot"),
            "window_screenshot": feature(
                x11 and capture == "import",
                "import -window" if x11 and capture == "import" else "needs ImageMagick on X11",
            ),
            "pointer": feature(input_ok, why_input),
            "keyboard": feature(input_ok, why_input),
            "background_input": feature(
                input_ok,
                "keyboard only, best effort: XSendEvent, which many applications ignore" if input_ok else why_input,
            ),
            "windows": feature(input_ok, why_input),
            "window_frame": feature(input_ok, why_input),
            "applications": feature(input_ok, "windows grouped by process" if input_ok else why_input),
            "menus": feature(tree, atspi_detail),
            "clipboard": feature(
                clip is not None, clip or ("install wl-clipboard" if session == "wayland" else "install xclip")
            ),
            "element_at": feature(tree, atspi_detail),
        }

    # ----------------------------------------------------------- applications
    def _window_ids(self, app: str | None = None) -> list[str]:
        args = ["search", "--onlyvisible", "--classname" if app else "--name", app or ""]
        code, out, _err = self.runner([self._xdotool("List windows"), *args], "List windows")
        ids = [line.strip() for line in out.splitlines() if line.strip().isdigit()] if code == 0 else []
        if not ids and app:  # WM_CLASS names are not always the process name
            code, out, _err = self.runner(
                [self._xdotool("List windows"), "search", "--onlyvisible", "--class", app], "List windows"
            )
            ids = [line.strip() for line in out.splitlines() if line.strip().isdigit()] if code == 0 else []
        return ids[:MAX_WINDOWS]

    def _window(self, window_id: str) -> dict[str, Any]:
        geometry = shell_values(self.xdo("getwindowgeometry", "--shell", window_id, what="Read a window"))
        title = self.xdo("getwindowname", window_id, what="Read a window").strip()
        code, pid_text, _err = self.runner([self._xdotool("Read a window"), "getwindowpid", window_id], "Read a window")
        pid = int(pid_text.strip()) if code == 0 and pid_text.strip().isdigit() else 0
        return {
            "id": int(window_id),
            "pid": pid,
            "app": process_name(pid) if pid else "",
            "title": title,
            "x": int(geometry.get("X", 0)),
            "y": int(geometry.get("Y", 0)),
            "width": int(geometry.get("WIDTH", 0)),
            "height": int(geometry.get("HEIGHT", 0)),
            "on_screen": True,
        }

    def op_frontmost(self) -> dict[str, Any]:
        active = self.xdo("getactivewindow", what="Find the active window").strip()
        window = self._window(active)
        return {"app": window["app"], "title": window["title"], "pid": window["pid"], "window": window}

    def op_running(self, app: str) -> dict[str, Any]:
        return {"app": app, "running": bool(running_pids(app))}

    def op_launch(self, app: str) -> dict[str, Any]:
        from highhx.computer.desktop import launch_command
        from highhx.core.errors import ToolNotFoundError

        try:
            argv = launch_command(app)
        except ToolNotFoundError:
            raise EngineError(
                "not_found", f"{app} is not installed (no program or desktop entry by that name)."
            ) from None
        self.run(argv, f"Launch {app}")
        return {"app": app, "running": self.poll(lambda: self.op_running(app)["running"], 10)}

    def op_focus(self, app: str) -> dict[str, Any]:
        ids = self._window_ids(app)
        if not ids:
            raise EngineError("not_found", f"{app} has no visible window.", hint=f"open {app} first")
        self.xdo("windowactivate", "--sync", ids[0], what=f"Switch to {app}")
        front = self.op_frontmost()["app"]
        return {"app": app, "frontmost": front.lower() == app.lower(), "actual": front}

    def op_open_url(self, url: str, app: str | None = None) -> dict[str, Any]:
        self.run([app, url] if app else ["xdg-open", url], f"Open {url}")
        return {"url": url, "app": app or "default browser"}

    def op_quit(self, app: str) -> dict[str, Any]:
        if not running_pids(app):
            raise EngineError("not_found", f"{app} is not running.")
        wmctrl = shutil.which("wmctrl")
        if wmctrl is None:
            raise EngineError(
                "unsupported_platform",
                "Closing an application politely needs wmctrl (xdotool can only destroy windows, losing unsaved work).",
                hint="Install it, e.g. `sudo apt install wmctrl`.",
            )
        for window_id in self._window_ids(app):
            self.run([wmctrl, "-i", "-c", hex(int(window_id))], f"Close {app}")
        gone = self.poll(lambda: not running_pids(app), 5)
        return {
            "app": app,
            "quit": gone,
            "detail": "" if gone else f"{app} is still running (it may be asking to save)",
        }

    def op_apps(self) -> dict[str, Any]:
        front = self.op_frontmost().get("pid")
        seen: dict[int, dict[str, Any]] = {}
        for window in self.op_windows()["windows"]:
            if window["pid"] and window["pid"] not in seen:
                seen[window["pid"]] = {
                    "name": window["app"],
                    "pid": window["pid"],
                    "bundle_id": "",
                    "frontmost": window["pid"] == front,
                    "visible": True,
                }
        return {"apps": list(seen.values())}

    def op_windows(self, app: str | None = None) -> dict[str, Any]:
        found = [self._window(i) for i in self._window_ids(app)]
        return {"windows": [w for w in found if w["width"] > 1 and w["height"] > 1]}

    def op_window_frame(self, window: int, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        wid = str(window)
        self.xdo("windowmove", "--sync", wid, str(x), str(y), what="Move the window")
        self.xdo("windowsize", "--sync", wid, str(width), str(height), what="Resize the window")
        after = self._window(wid)
        return {
            "window": window,
            "app": after["app"],
            "frame": [after["x"], after["y"], after["width"], after["height"]],
        }

    # ---------------------------------------------------------------- input
    def _target(self, app: str | None) -> list[str]:
        if not app:
            return []
        ids = self._window_ids(app)
        if not ids:
            raise EngineError("not_found", f"{app} has no visible window.")
        return ["--window", ids[0]]

    def op_type(self, text: str, app: str | None = None) -> dict[str, Any]:
        self.xdo("type", *self._target(app), "--clearmodifiers", "--delay", "8", "--", text, what="Type text")
        return {"characters": len(text), **({"app": app, "background": True} if app else {})}

    def op_key(self, key: str, app: str | None = None) -> dict[str, Any]:
        self.xdo("key", *self._target(app), "--clearmodifiers", keysym(key), what=f"Press {key}")
        return {"key": key, **({"app": app, "background": True} if app else {})}

    def op_hotkey(self, modifiers: list[str], key: str, app: str | None = None) -> dict[str, Any]:
        keys = combo(modifiers, key)
        self.xdo("key", *self._target(app), "--clearmodifiers", keys, what=f"Press {keys}")
        return {"keys": "+".join([*modifiers, key]), **({"app": app, "background": True} if app else {})}

    def op_scroll(self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        if x is not None and y is not None:
            self.xdo("mousemove", "--sync", str(x), str(y), what="Move the pointer")
        self.xdo("click", "--repeat", str(amount * 3), WHEEL[direction], what="Scroll")
        return {"direction": direction, "amount": amount, **({"x": x, "y": y} if x is not None else {})}

    def op_click_at(
        self, x: int, y: int, button: str = "left", count: int = 1, app: str | None = None
    ) -> dict[str, Any]:
        if app:
            raise EngineError(
                "unsupported_platform",
                "Pointer input cannot be delivered to a background window on X11 (only keyboard input can).",
            )
        self.xdo("mousemove", "--sync", str(x), str(y), what="Move the pointer")
        self.xdo("click", "--repeat", str(count), BUTTON_NUMBERS[button], what="Click")
        return {"x": x, "y": y, "button": button, "count": count}

    def op_move(self, x: int, y: int) -> dict[str, Any]:
        self.xdo("mousemove", "--sync", str(x), str(y), what="Move the pointer")
        return {"x": x, "y": y}

    def op_cursor(self) -> dict[str, Any]:
        values = shell_values(self.xdo("getmouselocation", "--shell", what="Read the pointer"))
        return {"x": int(values.get("X", 0)), "y": int(values.get("Y", 0))}

    def op_drag(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left", duration_ms: int = 300
    ) -> dict[str, Any]:
        number = BUTTON_NUMBERS[button]
        self.xdo("mousemove", "--sync", str(from_x), str(from_y), what="Drag")
        self.xdo("mousedown", number, what="Drag")
        steps = 8
        for step in range(1, steps + 1):
            px = from_x + (to_x - from_x) * step // steps
            py = from_y + (to_y - from_y) * step // steps
            self.xdo("mousemove", "--sync", str(px), str(py), what="Drag")
            self.sleep(duration_ms / 1000 / steps)
        self.xdo("mouseup", number, what="Drag")
        return {"from": [from_x, from_y], "to": [to_x, to_y], "button": button}

    # ---------------------------------------------------------- observation
    def op_screen(self) -> dict[str, Any]:
        width, height = self.xdo("getdisplaygeometry", what="Read the display").split()
        return {"width": int(width), "height": int(height), "x": 0, "y": 0, "scale": 1.0}

    def op_screenshot(self, path: str, window: int | None = None) -> dict[str, Any]:
        tool = self._capture_tool()
        if tool is None:
            raise EngineError(
                "unsupported_platform",
                "No screenshot tool is installed.",
                hint="Install grim (Wayland), ImageMagick or scrot.",
            )
        if window is not None and tool != "import":
            raise EngineError("unsupported_platform", "Capturing one window needs ImageMagick's import on X11.")
        argv = {
            "grim": ["grim", path],
            "import": ["import", "-window", str(window) if window is not None else "root", path],
            "scrot": ["scrot", "--overwrite", path],
            "gnome-screenshot": ["gnome-screenshot", "-f", path],
        }[tool]
        self.run(argv, "Capture the screen")
        if not Path(path).is_file():
            raise EngineError("failed", "The screen capture produced no file.")
        width, height = png_size(Path(path))
        return {"path": path, "width": width, "height": height, "scale": 1.0, "window": window}

    def op_clipboard_read(self) -> dict[str, Any]:
        argv = (
            ["wl-paste", "--no-newline"] if self.session() == "wayland" else ["xclip", "-o", "-selection", "clipboard"]
        )
        if shutil.which(argv[0]) is None:
            raise EngineError("unsupported_platform", f"Reading the clipboard needs {argv[0]}.")
        code, out, _err = self.runner(argv, "Read the clipboard")
        return {"text": out if code == 0 else "", "has_text": code == 0}

    def op_clipboard_write(self, text: str) -> dict[str, Any]:
        if self.session() == "wayland":
            if shutil.which("wl-copy") is None:
                raise EngineError("unsupported_platform", "Writing the clipboard needs wl-copy (wl-clipboard).")
            self.run(["wl-copy", "--", text], "Write the clipboard")
            return {"characters": len(text)}
        if shutil.which("xclip") is None:
            raise EngineError("unsupported_platform", "Writing the clipboard needs xclip.")
        with tempfile.TemporaryDirectory() as folder:  # private (0700); xclip reads the text from a file
            source = Path(folder) / "clipboard.txt"
            source.write_text(text, encoding="utf-8")
            self.run(["xclip", "-selection", "clipboard", "-i", str(source)], "Write the clipboard")
        return {"characters": len(text)}

    # ------------------------------------------------------------ AT-SPI
    def _app_node(self, api: Any, app: str | None) -> Any:
        name = (app or self.op_frontmost()["app"]).lower()
        desktop = api.get_desktop(0)
        for index in range(desktop.get_child_count()):
            node = desktop.get_child_at_index(index)
            if node is not None and (node.get_name() or "").lower() == name:
                return node
        raise EngineError("not_found", f"{app or name} is not in the accessibility tree (is it running?).")

    @staticmethod
    def _describe(api: Any, node: Any) -> dict[str, Any]:
        role_name = node.get_role_name() or ""
        secure = role_name == "password text"
        states = node.get_state_set()
        bounds = None
        try:
            extents = node.get_extents(api.CoordType.SCREEN)
            bounds = [extents.x, extents.y, extents.width, extents.height]
        except Exception:
            bounds = None
        value = ""
        if not secure:
            try:
                value = (node.get_text(0, -1) or "")[:300]
            except Exception:
                value = ""
        return {
            "role": ATSPI_ROLES.get(role_name, role_name.replace(" ", "")),
            "name": node.get_name() or "",
            "value": value,
            "enabled": states.contains(api.StateType.ENABLED),
            "focused": states.contains(api.StateType.FOCUSED),
            "secure": secure,
            "bounds": bounds if bounds and bounds[2] > 0 and bounds[3] > 0 else None,
        }

    def _walk(self, node: Any, limit: int) -> list[Any]:
        found: list[Any] = []
        queue = [node]
        while queue and len(found) < limit * 4:
            current = queue.pop(0)
            for index in range(current.get_child_count()):
                child = current.get_child_at_index(index)
                if child is not None:
                    found.append(child)
                    queue.append(child)
        return found

    def op_inspect(self, app: str | None = None, limit: int = 100) -> dict[str, Any]:
        api = self._tree("Reading the accessibility tree")
        root = self._app_node(api, app)
        elements = []
        for node in self._walk(root, limit):
            item = self._describe(api, node)
            if item["role"] in ATSPI_ROLES.values() and item["role"] != "window":
                elements.append(item)
            if len(elements) >= limit:
                break
        return {"app": root.get_name() or "", "title": "", "elements": elements}

    def op_click(self, name: str, role: str | None = None, app: str | None = None) -> dict[str, Any]:
        api = self._tree("Clicking an element")
        root = self._app_node(api, app)
        wanted = name.lower()
        matches = []
        for node in self._walk(root, 300):
            item = self._describe(api, node)
            if role not in (None, "any") and item["role"] != role:
                continue
            if item["name"].lower() == wanted:
                matches = [(node, item)]
                break
            if wanted in item["name"].lower():
                matches.append((node, item))
        if not matches:
            raise EngineError("not_found", f"No {role or 'element'} named {name!r} in {root.get_name()}.")
        if len(matches) > 1:
            names = ", ".join(repr(m[1]["name"]) for m in matches[:5])
            raise EngineError("not_found", f"{name!r} matches several elements ({names}); use the exact name.")
        node, item = matches[0]
        if node.get_n_actions() > 0:
            node.do_action(0)
        elif item["bounds"]:
            x, y, w, h = item["bounds"]
            self.op_click_at(x + w // 2, y + h // 2)
        else:
            raise EngineError("refused", f"{item['name']!r} has no action and no position to click.")
        return {"app": root.get_name() or "", "role": item["role"], "name": item["name"]}

    def op_element_at(self, x: int, y: int) -> dict[str, Any]:
        api = self._tree("Finding the element at a point")
        desktop = api.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            for w in range(app.get_child_count() if app is not None else 0):
                node = app.get_child_at_index(w)
                hit = None
                while node is not None:
                    try:
                        deeper = node.get_accessible_at_point(x, y, api.CoordType.SCREEN)
                    except Exception:
                        deeper = None
                    if deeper is None or deeper == node:
                        break
                    hit, node = deeper, deeper
                if hit is not None:
                    return {**self._describe(api, hit), "app": app.get_name() or "", "pid": app.get_process_id()}
        raise EngineError("not_found", f"No accessibility element at ({x}, {y}).")

    def op_menu(self, app: str, path: list[str]) -> dict[str, Any]:
        api = self._tree("Choosing a menu item")
        container = self._app_node(api, app)
        for level, name in enumerate(path):
            candidates = [
                n
                for n in self._walk(container, 300)
                if (n.get_role_name() or "") in ("menu", "menu item", "check menu item", "radio menu item")
            ]
            node = next((n for n in candidates if (n.get_name() or "") == name), None)
            if node is None:
                names = ", ".join(sorted({n.get_name() for n in candidates if n.get_name()})[:15])
                raise EngineError("not_found", f"{app} has no menu item {name!r} there (it has: {names}).")
            if node.get_n_actions() == 0:
                raise EngineError("refused", f"{' > '.join(path[: level + 1])} cannot be chosen.")
            node.do_action(0)
            self.sleep(0.2)
            container = node
        return {"app": app, "path": path}

    def op_verify(
        self, check: str, app: str | None = None, name: str | None = None, role: str | None = None
    ) -> dict[str, Any]:
        if check == "frontmost":
            front = self.op_frontmost()["app"]
            ok = bool(app) and front.lower() == str(app).lower()
            return {"ok": ok, "detail": f"{front} is frontmost"}
        if not app and check in ("running", "window"):
            raise EngineError("invalid_request", f"verify {check} needs app")
        if check == "running":
            ok = self.op_running(str(app))["running"]
            return {"ok": ok, "detail": f"{app} is {'running' if ok else 'not running'}"}
        if check == "window":
            titles = [w["title"] for w in self.op_windows(app)["windows"]]
            ok = bool(titles) and (not name or any(name.lower() in t.lower() for t in titles))
            return {"ok": ok, "detail": f"{len(titles)} window(s)", "windows": titles}
        if not name:
            raise EngineError("invalid_request", "verify element needs name")
        found = [e for e in self.op_inspect(app, 300)["elements"] if name.lower() in e["name"].lower()]
        return {"ok": bool(found), "detail": f"{name!r} {'found' if found else 'not found'}"}
