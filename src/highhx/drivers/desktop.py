"""Desktop drivers: the existing HighhX Computer API (``HighhXDriver``) adapted to ComputerDriver.

    DesktopDriver     any engine the bridge picked (.NET, or the built-in macOS/Windows/Linux backend)
    MacDriver · WindowsDriver · LinuxDriver
                      the same adapter, refusing to run on another platform (feature detection)
    RemoteDriver      a computer at the end of an SSH connection (``ssh://user@host``), operated
                      through the same bridge protocol by a remote ``highhx computer engine``

Nothing is reimplemented here: every operation is a HighhXDriver call, so the bridge's argument
validation, terminal guard and per-platform refusals apply unchanged.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from highhx.automation.engine.bridge import EngineError
from highhx.computer.providers import Capability
from highhx.drivers.base import CapabilityError, DriverCapabilities, DriverHelpers

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver
    from highhx.perception.state import ComputerState, ScreenshotRef, StateElement

P = ParamSpec("P")
R = TypeVar("R")

FEATURE_FOR = {
    "observe": "accessibility",
    "screenshot": "screenshot",
    "click": "pointer",
    "double_click": "pointer",
    "right_click": "pointer",
    "inspect": "element_at",
    "find": "accessibility",
    "wait": "",
    "move": "pointer",
    "drag": "pointer",
    "scroll": "pointer",
    "type": "keyboard",
    "key": "keyboard",
    "hotkey": "keyboard",
    "launch": "applications",
    "close": "applications",
    "focus": "applications",
}
"""Each ComputerDriver operation → the engine feature it needs (protocol FEATURES)."""


CAPABILITY_CODES = ("unsupported", "unsupported_platform", "accessibility_denied", "screen_recording_denied")


def _errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Engine refusals that mean "not possible here" become :class:`CapabilityError`."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except EngineError as exc:
            if exc.code in CAPABILITY_CODES:
                raise CapabilityError(exc.message, hint=exc.hint) from None
            raise

    return wrapper


class DesktopDriver(DriverHelpers):
    surface = "desktop"
    platform: str | None = None
    """Set by the platform subclasses: the ``sys.platform`` prefix they run on."""

    def __init__(self, driver: HighhXDriver, *, app: str | None = None) -> None:
        if self.platform is not None and not sys.platform.startswith(self.platform):
            raise CapabilityError(f"{type(self).__name__} runs on {self.platform}, not {sys.platform}.")
        self.driver = driver
        self.app = app
        self.name = f"desktop:{driver.session.target}"

    def capabilities(self) -> DriverCapabilities:
        features = self.driver.capabilities()
        out = {}
        for op, feature_name in FEATURE_FOR.items():
            if not feature_name:
                out[op] = Capability(op, True, "no engine feature needed")
                continue
            feature = features.get(feature_name)
            out[op] = (
                Capability(op, feature.available, feature.detail)
                if feature is not None
                else Capability(op, False, f"the engine does not report {feature_name}")
            )
        return DriverCapabilities(self.name, self.surface, out)

    @_errors
    def observe(self, *, screenshot: bool = False) -> ComputerState:
        from highhx.perception.engine import PerceptionEngine, PerceptionPolicy
        from highhx.perception.providers import DesktopAccessibility, DesktopScreenshots

        engine = PerceptionEngine(
            "desktop", structure=[DesktopAccessibility(self.driver, app=self.app)], screenshot=DesktopScreenshots(self.driver)
        )
        return engine.observe(policy=PerceptionPolicy(screenshot=screenshot, ocr="never", cache_ttl=0.0))

    def get_state(self) -> ComputerState:
        return self.observe()

    @_errors
    def screenshot(self) -> ScreenshotRef:
        from highhx.perception.providers import DesktopScreenshots

        return DesktopScreenshots(self.driver).capture()

    @_errors
    def click(self, x: int, y: int, *, button: str = "left") -> None:
        self.driver.click(x, y, button=button)

    @_errors
    def double_click(self, x: int, y: int) -> None:
        self.driver.click(x, y, count=2)

    @_errors
    def right_click(self, x: int, y: int) -> None:
        self.driver.click(x, y, button="right")

    @_errors
    def inspect(self, x: int, y: int) -> StateElement | None:
        from highhx.perception.state import StateElement

        element = self.driver.element_at(x, y)
        return StateElement.from_ui(element, source="ax") if element.role or element.name else None

    @_errors
    def type(self, text: str) -> None:
        self.driver.type_text(text)

    @_errors
    def key(self, key: str) -> None:
        self.driver.press(key)

    @_errors
    def hotkey(self, keys: str) -> None:
        self.driver.hotkey(keys)

    @_errors
    def scroll(self, direction: str, amount: int = 3, *, at: tuple[int, int] | None = None) -> None:
        self.driver.scroll(direction, amount, at=at)

    @_errors
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.driver.drag((x1, y1), (x2, y2))

    @_errors
    def move(self, x: int, y: int) -> None:
        self.driver.move(x, y)

    @_errors
    def launch(self, app: str) -> None:
        self.driver.launch(app)

    @_errors
    def close(self, app: str) -> None:
        self.driver.quit(app)

    @_errors
    def focus(self, app: str) -> None:
        self.driver.focus(app)


class MacDriver(DesktopDriver):
    platform = "darwin"


class WindowsDriver(DesktopDriver):
    platform = "win32"


class LinuxDriver(DesktopDriver):
    platform = "linux"


def local_driver_class() -> type[DesktopDriver]:
    if sys.platform == "darwin":
        return MacDriver
    if sys.platform.startswith("win"):
        return WindowsDriver
    if sys.platform.startswith("linux"):
        return LinuxDriver
    return DesktopDriver


class RemoteDriver(DesktopDriver):
    """A remote computer over SSH. The target must be ``ssh://…``. The connection, its checks and
    its loss handling are the existing remote engine's (``automation/engine/remote.py``)."""

    def __init__(self, driver: HighhXDriver, *, app: str | None = None) -> None:
        if not str(driver.session.target).startswith("ssh://"):
            raise CapabilityError(f"RemoteDriver needs an ssh:// target, not {driver.session.target!r}.")
        super().__init__(driver, app=app)
