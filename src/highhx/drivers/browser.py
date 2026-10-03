"""BrowserDriver: the HighhX browser (Chrome DevTools) as a ComputerDriver.

Coordinates are CSS pixels of the visible viewport (what ``page_capture`` screenshots show).
Each operation is a ``ChromeBrowser`` call, so its single recovery policy applies. Unsafe input
(clicks, typing, keys) is never repeated after a failure that may have delivered it.
Applications are not a browser concept: ``launch`` / ``focus`` open or switch to a tab, and
``close`` closes the current tab.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from highhx.computer.providers import Capability
from highhx.drivers.base import OPERATIONS, CapabilityError, DriverCapabilities

if TYPE_CHECKING:
    from highhx.computer.browser import ChromeBrowser
    from highhx.execution.cancellation import CancellationToken
    from highhx.perception.state import ComputerState, ScreenshotRef

MODIFIERS = {"cmd": "command", "command": "command", "ctrl": "control", "control": "control", "alt": "option", "option": "option", "shift": "shift"}


class BrowserDriver:
    surface = "browser"

    def __init__(self, browser: ChromeBrowser, *, cancel: CancellationToken | None = None) -> None:
        self.browser = browser
        self.cancel = cancel
        self.name = "browser:chrome"

    def capabilities(self) -> DriverCapabilities:
        cap = self.browser.capability()
        features = {op: Capability(op, cap.available, cap.detail) for op in OPERATIONS}
        return DriverCapabilities(self.name, self.surface, features)

    def observe(self, *, screenshot: bool = False) -> ComputerState:
        from highhx.perception.engine import PerceptionEngine, PerceptionPolicy
        from highhx.perception.providers import BrowserDOM, BrowserScreenshots

        engine = PerceptionEngine("browser", structure=[BrowserDOM(self.browser)], screenshot=BrowserScreenshots(self.browser))
        state = engine.observe(policy=PerceptionPolicy(screenshot=screenshot, ocr="never", cache_ttl=0.0), cancel=self.cancel)
        try:
            view = self.browser.viewport(cancel=self.cancel)
        except Exception:  # the viewport is context; the page itself was observed
            return state
        width, height = int(view.get("width") or 0), int(view.get("height") or 0)
        return state.with_(viewport=(width, height)) if width and height else state

    def get_state(self) -> ComputerState:
        return self.observe()

    def screenshot(self) -> ScreenshotRef:
        from highhx.perception.state import ScreenshotRef

        data, _view = self.browser.page_capture(cancel=self.cancel)
        return ScreenshotRef.from_bytes(data)

    def click(self, x: int, y: int, *, button: str = "left") -> None:
        self.browser.pointer("click", x, y, button=button, cancel=self.cancel)

    def double_click(self, x: int, y: int) -> None:
        self.browser.pointer("click", x, y, count=2, cancel=self.cancel)

    def type(self, text: str) -> None:
        self.browser.insert_text(text, cancel=self.cancel)

    def key(self, key: str) -> None:
        self.browser.press(key.lower(), cancel=self.cancel)

    def hotkey(self, keys: str) -> None:
        parts = [p.strip().lower() for p in keys.replace("+", " ").split() if p.strip()]
        if not parts:
            raise CapabilityError("no keys given")
        *mods, key = parts
        unknown = [m for m in mods if m not in MODIFIERS]
        if unknown:
            raise CapabilityError(f"unknown modifier(s): {', '.join(unknown)}")
        self.browser.key_combo([MODIFIERS[m] for m in mods], key, cancel=self.cancel)

    def scroll(self, direction: str, amount: int = 3, *, at: tuple[int, int] | None = None) -> None:
        if direction not in ("up", "down"):
            raise CapabilityError(f"the browser scrolls up or down, not {direction}")
        if at is not None:
            self.browser.pointer("wheel", at[0], at[1], direction=direction, cancel=self.cancel)
        else:
            self.browser.scroll(direction, cancel=self.cancel)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.browser.pointer("drag", x1, y1, to=(x2, y2), cancel=self.cancel)

    def move(self, x: int, y: int) -> None:
        self.browser.pointer("move", x, y, cancel=self.cancel)

    def launch(self, app: str) -> None:
        self.browser.new_tab(app if "://" in app else f"https://{app}", cancel=self.cancel)

    def close(self, app: str) -> None:
        self.browser.close_tab(cancel=self.cancel)

    def focus(self, app: str) -> None:
        self.browser.switch_tab(app, cancel=self.cancel)
