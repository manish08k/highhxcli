"""The desktop computer-use provider: the computer runtime's view of the HighhX Computer API.

The computer runtime (:mod:`highhx.computer.runtime`) resolves elements, classifies each one
(a "Delete" or "Send" button asks), acts and re-observes to verify. With this provider it
does all of that through :class:`~highhx.computer.driver.HighhXDriver` — so desktop actions
keep the runtime's per-element safety, and the engine only performs the operation. Presses go
to the element by name (semantic targeting); double clicks, right clicks, hovering and dragging
use the element's accessibility bounds (and refuse when the element reports none).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from highhx.automation.engine.protocol import ROLES
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.core.errors import IntegrationError
from highhx.execution.cancellation import CancellationToken

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver


class BridgeDesktopProvider:
    name = "accessibility"

    def __init__(self, driver: HighhXDriver, application: str | None = None) -> None:
        self.driver = driver
        self.application = application
        self._last: dict[str, UIElement] = {}

    def capability(self) -> Capability:
        status = self.driver.status()
        return Capability(self.name, bool(status.get("ok")), str(status.get("detail") or ""))

    def activate(self, application: str, *, cancel: CancellationToken | None = None) -> None:
        self.application = application
        self.driver.focus(application)

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        observation = self.driver.get_ui_tree(self.application)
        self._last = {e.id: e for e in observation.elements}
        return observation

    def _element(self, element_id: str) -> UIElement:
        element = self._last.get(element_id)
        if element is None:
            raise IntegrationError(f"Element {element_id} is not in the latest observation.")
        return element

    def _center(self, element_id: str) -> tuple[int, int]:
        element = self._element(element_id)
        if element.bounds is None:
            raise IntegrationError(f"{element.label()} reports no position, so it cannot be pointed at.")
        x, y, width, height = element.bounds
        return x + width // 2, y + height // 2

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        element = self._element(element_id)
        role = element.role if element.role in ROLES else "any"
        self.driver.press_element(element.name, role=role, app=self.application)

    def double_click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self.driver.double_click(*self._center(element_id))

    def right_click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self.driver.right_click(*self._center(element_id))

    def hover(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self.driver.move(*self._center(element_id))

    def drag(self, element_id: str, target: str, *, cancel: CancellationToken | None = None) -> None:
        """Drag one element onto another (named by id or by its exact accessible name)."""
        destination = self._last.get(target) or next(
            (e for e in self._last.values() if e.name and e.name.lower() == target.strip().lower()), None
        )
        if destination is None:
            raise IntegrationError(f"There is no element {target!r} to drag onto.")
        self.driver.drag(self._center(element_id), self._center(destination.id))

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self.click(element_id, cancel=cancel)  # focus the field first
        self.driver.type_text(text)

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        self.driver.press(key)

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        self.driver.scroll(direction, 3)

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self.type_text(element_id, option, cancel=cancel)
