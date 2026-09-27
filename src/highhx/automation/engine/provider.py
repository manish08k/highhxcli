"""A desktop computer-use provider over the automation bridge.

The computer runtime (:mod:`highhx.computer.runtime`) resolves elements, classifies each one
(a "Delete" or "Send" button asks), acts and re-observes to verify. With this provider it
does all of that through the automation bridge — so desktop clicks keep the runtime's
per-element safety, and the engine (C#/.NET or Python) only performs the operation.
"""

from __future__ import annotations

import time

from highhx.automation.engine.bridge import AutomationBridge
from highhx.automation.engine.protocol import ROLES
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.core.errors import IntegrationError
from highhx.execution.cancellation import CancellationToken


class BridgeDesktopProvider:
    name = "accessibility"

    def __init__(self, bridge: AutomationBridge, application: str | None = None) -> None:
        self.bridge = bridge
        self.application = application
        self._last: dict[str, UIElement] = {}

    def capability(self) -> Capability:
        status = self.bridge.call("status")
        return Capability(self.name, bool(status.get("ok")), str(status.get("detail") or ""))

    def activate(self, application: str, *, cancel: CancellationToken | None = None) -> None:
        self.application = application
        self.bridge.call("focus", app=application)

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        data = self.bridge.call("inspect", **({"app": self.application} if self.application else {}), limit=300)
        elements = [
            UIElement(
                id=f"a{n}",
                role=str(item.get("role") or ""),
                name=str(item.get("name") or ""),
                value=str(item.get("value") or ""),
                enabled=bool(item.get("enabled", True)),
                focused=bool(item.get("focused")),
                source="ax",
            )
            for n, item in enumerate(data.get("elements") or [], start=1)
        ]
        self._last = {e.id: e for e in elements}
        return Observation(
            provider=self.name,
            application=str(data.get("app") or self.application or ""),
            title=str(data.get("title") or ""),
            elements=elements,
            captured_at=time.time(),
        )

    def _element(self, element_id: str) -> UIElement:
        element = self._last.get(element_id)
        if element is None:
            raise IntegrationError(f"Element {element_id} is not in the latest observation.")
        return element

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        element = self._element(element_id)
        role = element.role if element.role in ROLES else "any"
        self.bridge.call(
            "click", name=element.name, role=role, **({"app": self.application} if self.application else {})
        )

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self.click(element_id, cancel=cancel)  # focus the field first
        self.bridge.call("type", text=text)

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        self.bridge.call("key", key=key)

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        self.bridge.call("scroll", direction=direction, amount=3)

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self.type_text(element_id, option, cancel=cancel)
