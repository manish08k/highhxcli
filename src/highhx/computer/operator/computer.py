"""The computer as a vision agent operates it: screenshots, GUI actions, state, verification.

    ComputerOperator
      screenshot()        a grounded capture (computer.screenshot → the session's CaptureStore)
      execute(action, c)  one GuiAction → one computer.* catalog action, in capture c's coordinates
      state()             frontmost application and window, the windows, the engine and its target
      focus_window(id)    computer.focus with window
      verify(expect)      computer.verify (bounded predicates, satisfied / unsatisfied / unknown)
      connection()        whether the computer can be operated now, and where it is

Everything runs through the given :class:`~highhx.actions.executor.ActionExecutor` — risk,
approval, audit, the terminal guard, capture grounding and stale-screenshot refusal apply exactly
as for ``highhx computer`` and the agent. The operator adds no way to act that they do not have.
Local and remote computers are the same operator: the session's driver decides where it runs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from highhx.computer.operator.parse import GuiAction

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.actions.spec import ActionResult
    from highhx.computer.capture import Capture


@dataclass
class Connection:
    name: str
    state: str
    """connected | unavailable | error"""
    detail: str
    target: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def primary_modifier(platform: str = sys.platform) -> str:
    return "cmd" if platform == "darwin" else "ctrl"


class ComputerOperator:
    kind = "desktop"

    def __init__(self, executor: ActionExecutor, *, window_app: str | None = None, space: str = "pixels") -> None:
        self.executor = executor
        self.window_app = window_app
        """Screenshots of this application's front window instead of the whole screen."""
        self.space = space
        """How the model states coordinates: ``pixels`` of the image, or ``relative1000``."""

    # ---------------------------------------------------------------- observing
    def screenshot(self, *, max_size: int | None = None) -> Capture:
        from highhx.computer.capture import MODEL_MAX_SIZE

        inputs: dict[str, Any] = {"max_size": max_size or MODEL_MAX_SIZE}
        if self.window_app:
            inputs["app"] = self.window_app
        result = self.executor.run("computer.screenshot", inputs)
        if not result.ok:
            from highhx.core.errors import IntegrationError

            raise IntegrationError(f"No screenshot: {result.error or result.status}")
        return self.executor.computer().captures.get(str(result.output["capture"]))

    def state(self) -> dict[str, Any]:
        """What is in front now — facts for the model and the person, read fresh."""
        driver = self.executor.driver()
        app, title, window = driver.active()
        windows = driver.windows()
        return {
            "app": app,
            "title": title,
            "window": window.id if window else (windows[0].id if windows else None),
            "windows": [
                {"id": w.id, "app": w.app, "title": w.title, "frame": [w.x, w.y, w.width, w.height]}
                for w in windows[:12]
            ],
            "engine": driver.name,
            "target": driver.session.target,
        }

    def connection(self) -> Connection:
        try:
            driver = self.executor.driver()
            status = driver.status()
        except Exception as exc:  # the engine could not start: said, not hidden
            return Connection("computer", "error", str(getattr(exc, "message", exc)))
        target = driver.session.target
        if not status.get("ok", True):
            return Connection("computer", "unavailable", str(status.get("detail") or ""), target)
        return Connection("computer", "connected", f"{driver.name} engine", target)

    # ---------------------------------------------------------------- acting
    def focus_window(self, window: int) -> ActionResult:
        return self.executor.run("computer.focus", {"window": window})

    def verify(self, expect: list[dict[str, Any]], *, timeout_ms: int = 3000) -> ActionResult:
        return self.executor.run("computer.verify", {"expect": expect, "timeout_ms": timeout_ms})

    def execute(self, action: GuiAction, capture: Capture) -> ActionResult:
        """One GUI action as the matching computer.* action; points are pixels (or 0-1000
        positions) in ``capture``, which the executor grounds — or refuses as stale."""
        name, inputs = self.plan(action, capture)
        return self.executor.run(name, inputs)

    def plan(self, action: GuiAction, capture: Capture) -> tuple[str, dict[str, Any]]:
        """(catalog action, inputs) for ``action`` — pure data; nothing runs here."""
        at = {"capture": capture.id, "space": self.space}
        point = action.point
        kind = action.kind

        def xy(p: tuple[float, float] | None) -> dict[str, int]:
            assert p is not None
            return {"x": round(p[0]), "y": round(p[1])}

        if kind in ("click", "double_click", "right_click"):
            extra = {"double_click": {"count": 2}, "right_click": {"button": "right"}}.get(kind, {})
            return "computer.click_at", {**at, **xy(point), **extra}
        if kind == "hover":
            return "computer.move", {**at, **xy(point)}
        if kind in ("mouse_down", "mouse_up"):
            return "computer.mouse_button", {**at, **xy(point), "action": kind.removeprefix("mouse_")}
        if kind == "drag":
            start, end = xy(point), xy(action.end)
            return "computer.drag", {
                **at,
                "from_x": start["x"],
                "from_y": start["y"],
                "to_x": end["x"],
                "to_y": end["y"],
            }
        if kind == "scroll":
            where = {**at, **xy(point)} if point is not None else {}
            return "computer.scroll", {"source": "desktop", "direction": action.direction, "amount": 3, **where}
        if kind == "type":
            return "computer.type", {"text": action.text}
        if kind == "key":
            return "computer.press", {"key": action.keys}
        if kind == "hotkey":
            return "computer.hotkey", {"keys": action.keys}
        if kind in ("copy", "paste"):
            return "computer.hotkey", {"keys": f"{primary_modifier()}+{'c' if kind == 'copy' else 'v'}"}
        if kind == "open_app":
            return "computer.focus", {"app": action.app}  # launches it when it is not running
        if kind == "focus_window":
            return "computer.focus", {"window": action.window}
        if kind == "close_window":
            return "computer.hotkey", {"keys": "cmd+w" if sys.platform == "darwin" else "alt+f4"}
        if kind == "minimize_window":
            if sys.platform != "darwin":
                raise ValueError("minimizing needs the window's own control here: click its minimize button")
            return "computer.hotkey", {"keys": "cmd+m"}
        if kind == "maximize_window":
            front = self.state()
            app = str(front.get("app") or "")
            if sys.platform == "darwin":
                return "computer.menu", {"app": app, "path": "Window > Zoom"}
            screen = self.executor.driver().screen()
            return "computer.window", {"app": app, "x": screen.x, "y": screen.y, "width": screen.width, "height": screen.height}
        raise ValueError(f"{kind} is not a computer action")
