"""A deterministic desktop for computer-use tasks: applications with windows, a menu bar, UI
elements with bounds and state (text fields hold values, checkboxes toggle, buttons count),
keyboard focus, the pointer and the clipboard. It speaks the automation protocol, so a task
runs through the real agent, approvals, executor, driver and bridge — only the OS is simulated."""

from __future__ import annotations

import copy
from typing import Any

from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.platforms import choose_element
from highhx.automation.engine.protocol import FEATURES


class SimulatedDesktop:
    name = "simulated"

    def __init__(self, setup: dict[str, Any]) -> None:
        state = copy.deepcopy(setup)
        self.front: str = state["front"]
        self.apps: dict[str, dict[str, Any]] = state["apps"]
        self.clipboard: str = state.get("clipboard", "")
        self.pointer: tuple[int, int] = (0, 0)
        self.focused: str | None = None
        self.log: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------ state
    def app(self, name: str | None = None) -> dict[str, Any]:
        found = self.apps.get(name or self.front)
        if found is None or not found.get("running", True):
            raise EngineError("not_found", f"{name or self.front} is not running")
        return found

    def element(self, name: str, app: str | None = None) -> dict[str, Any]:
        for element in self.app(app)["elements"]:
            if element["name"].lower() == name.lower():
                return element
        raise EngineError("not_found", f"no element named {name!r}")

    def _at(self, x: int, y: int) -> dict[str, Any] | None:
        for element in self.app()["elements"]:
            ex, ey, width, height = element["bounds"]
            if ex <= x < ex + width and ey <= y < ey + height:
                return element
        return None

    def _activate(self, element: dict[str, Any]) -> None:
        kind = element["role"]
        if kind == "button":
            element["presses"] = element.get("presses", 0) + 1
        elif kind == "checkbox":
            element["checked"] = not element.get("checked", False)
        self.focused = element["name"] if kind in ("textbox", "searchbox") else self.focused

    def _type(self, text: str, app: str | None = None) -> None:
        if self.focused is None:
            raise EngineError("failed", "no text field has keyboard focus")
        field = self.element(self.focused, app)
        field["value"] = field.get("value", "") + text

    # ---------------------------------------------------------- protocol
    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.log.append((op, args))
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise EngineError("unsupported_platform", f"{op} is not simulated")
        result: dict[str, Any] = handler(**args)
        return result

    def close(self) -> None:
        return None

    def op_status(self) -> dict[str, Any]:
        return {"engine": self.name, "ok": True, "accessibility": True, "detail": "simulated desktop"}

    def op_capabilities(self) -> dict[str, Any]:
        return {"platform": "simulated", "features": {f: {"available": True, "detail": "simulated"} for f in FEATURES}}

    def op_frontmost(self) -> dict[str, Any]:
        return {"app": self.front, "title": self.app()["window"]["title"], "window": self.app()["window"]}

    def op_running(self, app: str) -> dict[str, Any]:
        return {"app": app, "running": bool(self.apps.get(app, {}).get("running", app in self.apps))}

    def op_focus(self, app: str) -> dict[str, Any]:
        self.app(app)
        self.front, self.focused = app, None
        return {"app": app, "frontmost": True, "actual": app}

    def op_inspect(self, app: str | None = None, limit: int = 100) -> dict[str, Any]:
        target = self.app(app)
        elements = [
            {
                "role": e["role"],
                "name": e["name"],
                "value": e.get("value", ""),
                "enabled": True,
                "focused": e["name"] == self.focused,
                "bounds": e["bounds"],
            }
            for e in target["elements"][:limit]
        ]
        return {"app": app or self.front, "title": target["window"]["title"], "elements": elements}

    def op_click(
        self,
        name: str,
        role: str | None = None,
        app: str | None = None,
        index: int | None = None,
        bounds: list[int] | None = None,
    ) -> dict[str, Any]:
        elements = self.app(app)["elements"]
        element = choose_element(
            [(e, e["role"], e["name"], e["bounds"]) for e in elements], name, role, index=index, bounds=bounds
        )
        self._activate(element)
        return {"app": app or self.front, "role": element["role"], "name": element["name"]}

    def op_click_at(
        self, x: int, y: int, button: str = "left", count: int = 1, app: str | None = None
    ) -> dict[str, Any]:
        self.pointer = (x, y)
        element = self._at(x, y)
        if element is not None and button == "left":
            for _ in range(count):
                self._activate(element)
        return {"x": x, "y": y, "button": button, "count": count, "element": element}

    def op_type(self, text: str, app: str | None = None) -> dict[str, Any]:
        self._type(text, app)
        return {"characters": len(text)}

    def op_key(self, key: str, app: str | None = None) -> dict[str, Any]:
        return {"key": key}

    def op_hotkey(self, modifiers: list[str], key: str, app: str | None = None) -> dict[str, Any]:
        if modifiers == ["command"] and key == "v":
            self._type(self.clipboard, app)
        elif modifiers == ["command"] and key == "s":
            self.app(app)["saved"] = True
        return {"keys": "+".join([*modifiers, key])}

    def op_menu(self, app: str, path: list[str]) -> dict[str, Any]:
        if path == ["Window", "Minimize"]:  # every macOS application has it
            self.app(app)["window"]["minimized"] = True
            return {"app": app, "path": path}
        items = self.app(app).get("menus", {}).get(path[0], [])
        if len(path) != 2 or path[1] not in items:
            raise EngineError("not_found", f"{app} has no menu item {path[-1]!r}")
        if path == ["File", "Save"]:
            self.app(app)["saved"] = True
        return {"app": app, "path": path}

    def op_windows(self, app: str | None = None) -> dict[str, Any]:
        return {
            "windows": [
                {k: v for k, v in a["window"].items() if k != "minimized"}
                for n, a in self.apps.items()
                if a.get("running", True) and not a["window"].get("minimized") and (not app or n == app)
            ]
        }

    def op_window_frame(self, window: int, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        for name, found in self.apps.items():
            if found["window"]["id"] == window:
                found["window"].update(x=x, y=y, width=width, height=height)
                return {"window": window, "app": name, "frame": [x, y, width, height]}
        raise EngineError("not_found", f"no window {window}")

    def op_quit(self, app: str) -> dict[str, Any]:
        target = self.app(app)
        if not target.get("saved", True):
            return {"app": app, "quit": False, "detail": f"{app} is asking to save"}
        target["running"] = False
        return {"app": app, "quit": True, "detail": ""}

    def op_clipboard_read(self) -> dict[str, Any]:
        return {"text": self.clipboard, "has_text": bool(self.clipboard)}

    def op_clipboard_write(self, text: str) -> dict[str, Any]:
        self.clipboard = text
        return {"characters": len(text)}

    def op_cursor(self) -> dict[str, Any]:
        return {"x": self.pointer[0], "y": self.pointer[1]}

    def op_screen(self) -> dict[str, Any]:
        return {"width": 1440, "height": 900, "x": 0, "y": 0, "scale": 1.0}
