"""ComputerDriver: one interface for every computer HighhX can operate.

    observe() · screenshot() · get_state() · inspect(x, y) · find(target)
    click(x, y) · double_click(x, y) · right_click(x, y) · type(text) · key(name) · hotkey(keys)
    scroll(direction) · drag(x1, y1, x2, y2) · move(x, y) · wait(seconds)
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
    from highhx.grounding.hybrid import GroundingResult
    from highhx.perception.state import ComputerState, ScreenshotRef, StateElement

OPERATIONS = (
    "observe",
    "screenshot",
    "inspect",
    "find",
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "hotkey",
    "scroll",
    "drag",
    "move",
    "wait",
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

    def right_click(self, x: int, y: int) -> None: ...

    def inspect(self, x: int, y: int) -> StateElement | None: ...

    def find(self, target: str, *, role: str = "") -> GroundingResult: ...

    def wait(self, seconds: float) -> None: ...

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


class DriverHelpers:
    """``inspect``, ``find`` and ``wait`` for any driver with ``observe()``: the element at a point,
    a target grounded on a fresh observation (hybrid grounding, never a guess), a pause."""

    def observe(self, *, screenshot: bool = False) -> ComputerState:  # pragma: no cover - provided by the driver
        raise NotImplementedError

    def inspect(self, x: int, y: int) -> StateElement | None:
        return self.observe().at((x, y))

    def find(self, target: str, *, role: str = "") -> GroundingResult:
        from highhx.grounding import HybridGrounder, Target

        return HybridGrounder().ground(self.observe(), Target.of(target, role), strategies=("accessibility", "dom", "text"))

    def wait(self, seconds: float) -> None:
        import time

        time.sleep(max(0.0, min(float(seconds), 300.0)))
