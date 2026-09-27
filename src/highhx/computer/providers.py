"""Computer-use provider interfaces.

Perception is accessibility-first:

    1. native accessibility / UI automation   (AccessibilityProvider)
    2. browser DOM / structured browser state (BrowserAutomationProvider)
    3. local OCR                              (OCRProvider)
    4. a vision model, optionally             (VisionProvider)

Every provider speaks the same semantic model (``Observation`` / ``UIElement``)
and performs actions on element ids from its latest observation. Platform
specifics live behind these interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from highhx.computer.model import Observation
from highhx.execution.cancellation import CancellationToken


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    detail: str
    """Why it is (not) available, e.g. 'Chrome 141 at …' or 'grant Accessibility permission'."""


@runtime_checkable
class ComputerUseProvider(Protocol):
    """Anything that can observe a UI semantically and act on its elements."""

    name: str

    def capability(self) -> Capability: ...

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation: ...

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None: ...

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None: ...

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None: ...

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None: ...

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None: ...


class AccessibilityProvider(ComputerUseProvider, Protocol):
    """Native desktop UI automation (macOS Accessibility, Windows UI Automation, Linux AT-SPI)."""

    def activate(self, application: str, *, cancel: CancellationToken | None = None) -> None: ...


class BrowserAutomationProvider(ComputerUseProvider, Protocol):
    """Structured browser automation (DOM + accessibility names)."""

    def navigate(self, url: str, *, cancel: CancellationToken | None = None) -> None: ...

    def close(self) -> None: ...


class OCRProvider(Protocol):
    """Reads text and its position from the screen when no structured UI is available."""

    name: str

    def capability(self) -> Capability: ...

    def read_screen(self, *, cancel: CancellationToken | None = None) -> Observation: ...


class VisionProvider(Protocol):
    """Optional fallback: a vision model that turns a screenshot into an observation.

    HighhX ships no vision provider; structured perception (accessibility, DOM) is used
    instead. A plugin can implement this protocol.
    """

    name: str

    def capability(self) -> Capability: ...

    def describe_screen(self, *, cancel: CancellationToken | None = None) -> Observation: ...


BrowserProvider = BrowserAutomationProvider
"""Alias: the browser flavour of :class:`ComputerUseProvider`."""
