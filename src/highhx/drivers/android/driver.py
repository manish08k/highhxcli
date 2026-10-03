"""AndroidDriver: a phone, tablet or emulator through adb, as a ComputerDriver.

Coordinates are device pixels (what ``screencap`` and the uiautomator bounds use). There is no
pointer to move and no modifier keys, so ``move`` and ``hotkey`` report that instead of
pretending. ``launch`` / ``close`` / ``focus`` take package names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from highhx.computer.providers import Capability
from highhx.drivers.android.adb import AdbClient
from highhx.drivers.base import OPERATIONS, CapabilityError, DriverCapabilities

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState, ScreenshotRef

NOT_ON_ANDROID = {
    "move": "Android has no pointer to move",
    "hotkey": "Android has no modifier-key shortcuts; send key events instead",
}


class AndroidDriver:
    surface = "android"

    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb
        self.name = f"android:{adb.serial or 'default'}"

    def capabilities(self) -> DriverCapabilities:
        cap = self.adb.capability()
        features = {
            op: Capability(op, False, NOT_ON_ANDROID[op]) if op in NOT_ON_ANDROID else Capability(op, cap.available, cap.detail)
            for op in OPERATIONS
        }
        for extra in ("long_press", "swipe", "back", "home", "install", "packages"):
            features[extra] = Capability(extra, cap.available, cap.detail)
        return DriverCapabilities(self.name, self.surface, features)

    def observe(self, *, screenshot: bool = False) -> ComputerState:
        from highhx.drivers.android.perception import AndroidHierarchy, AndroidScreenshots
        from highhx.perception.engine import PerceptionEngine, PerceptionPolicy

        engine = PerceptionEngine("android", structure=[AndroidHierarchy(self.adb)], screenshot=AndroidScreenshots(self.adb))
        return engine.observe(policy=PerceptionPolicy(screenshot=screenshot, ocr="never", cache_ttl=0.0))

    def get_state(self) -> ComputerState:
        return self.observe()

    def screenshot(self) -> ScreenshotRef:
        from highhx.perception.state import ScreenshotRef

        return ScreenshotRef.from_bytes(self.adb.screencap())

    def click(self, x: int, y: int, *, button: str = "left") -> None:
        if button != "left":
            raise CapabilityError("Android taps have no buttons; use long_press for a context action.")
        self.adb.tap(x, y)

    def double_click(self, x: int, y: int) -> None:
        self.adb.tap(x, y)
        self.adb.tap(x, y)

    def long_press(self, x: int, y: int, ms: int = 800) -> None:
        self.adb.long_press(x, y, ms)

    def type(self, text: str) -> None:
        self.adb.text(text)

    def key(self, key: str) -> None:
        self.adb.keyevent(key)

    def hotkey(self, keys: str) -> None:
        raise CapabilityError(NOT_ON_ANDROID["hotkey"])

    def scroll(self, direction: str, amount: int = 3, *, at: tuple[int, int] | None = None) -> None:
        size = self.adb.screen_size() or (1080, 1920)
        cx, cy = at or (size[0] // 2, size[1] // 2)
        span = max(1, min(amount, 10)) * size[1] // 12
        moves = {
            "down": (cx, cy + span // 2, cx, cy - span // 2),  # content moves up: finger moves up
            "up": (cx, cy - span // 2, cx, cy + span // 2),
            "right": (cx + span // 2, cy, cx - span // 2, cy),
            "left": (cx - span // 2, cy, cx + span // 2, cy),
        }
        if direction not in moves:
            raise CapabilityError(f"unknown scroll direction {direction!r}")
        self.adb.swipe(*moves[direction], ms=350)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.adb.swipe(x1, y1, x2, y2, ms)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.adb.swipe(x1, y1, x2, y2, 900)

    def move(self, x: int, y: int) -> None:
        raise CapabilityError(NOT_ON_ANDROID["move"])

    def launch(self, app: str) -> None:
        package, _, activity = app.partition("/")
        self.adb.launch(package, activity or None)

    def close(self, app: str) -> None:
        self.adb.stop(app)

    def focus(self, app: str) -> None:
        self.adb.launch(app)

    def back(self) -> None:
        self.adb.keyevent("back")

    def home(self) -> None:
        self.adb.keyevent("home")
