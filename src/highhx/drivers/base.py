"""ComputerDriver: one interface for every computer HighhX can operate.

    observe() · screenshot() · get_state()
    click(x, y) · double_click(x, y) · type(text) · key(name) · hotkey(keys)
    scroll(direction) · drag(x1, y1, x2, y2) · move(x, y)
    launch(app) · close(app) · focus(app)
    capabilities()                    which of these work here, and why not

A driver is mechanism, not policy. Catalog action handlers call drivers after the executor has
classified, policy-checked and approved the action. An agent never holds a driver. A driver
that cannot do something raises :class:`CapabilityError` with the reason and what to install or
grant. It never pretends.

Coordinates are the surface's input space: desktop points, CSS pixels in a browser viewport,
device pixels on Android.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from highhx.computer.providers import Capability
from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState, ScreenshotRef

OPERATIONS = (
    "observe",
    "screenshot",
    "click",
    "double_click",
    "type",
    "key",
    "hotkey",
    "scroll",
    "drag",
    "move",
    "launch",
    "close",
    "focus",
)


class CapabilityError(HighhXError):
    """The driver cannot do this here (platform, missing tool, permission)."""


@dataclass(frozen=True)
class DriverCapabilities:
    driver: str
    surface: str
    features: dict[str, Capability] = field(default_factory=dict)

    def supports(self, operation: str) -> bool:
        feature = self.features.get(operation)
        return feature is not None and feature.available

    def require(self, operation: str) -> None:
        feature = self.features.get(operation)
        if feature is None or not feature.available:
            detail = feature.detail if feature is not None else "not implemented by this driver"
            raise CapabilityError(f"{self.driver} cannot {operation.replace('_', ' ')}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "surface": self.surface,
            "features": {k: {"available": v.available, "detail": v.detail} for k, v in self.features.items()},
        }


@runtime_checkable
class ComputerDriver(Protocol):
    name: str
    surface: str

    def capabilities(self) -> DriverCapabilities: ...

    def observe(self, *, screenshot: bool = False) -> ComputerState: ...

    def get_state(self) -> ComputerState: ...

    def screenshot(self) -> ScreenshotRef: ...

    def click(self, x: int, y: int, *, button: str = "left") -> None: ...

    def double_click(self, x: int, y: int) -> None: ...

    def type(self, text: str) -> None: ...

    def key(self, key: str) -> None: ...

    def hotkey(self, keys: str) -> None: ...

    def scroll(self, direction: str, amount: int = 3, *, at: tuple[int, int] | None = None) -> None: ...

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...

    def move(self, x: int, y: int) -> None: ...

    def launch(self, app: str) -> None: ...

    def close(self, app: str) -> None: ...

    def focus(self, app: str) -> None: ...


def unsupported(driver: str, detail: str) -> dict[str, Capability]:
    return {op: Capability(op, False, detail) for op in OPERATIONS}
