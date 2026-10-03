"""Perception sources: model- and platform-neutral interfaces, plus adapters over HighhX's drivers.

    StructureProvider     a structured UI tree: DOMProvider (browser) · AccessibilityProvider
                          (desktop AX/UIA/AT-SPI) · the Android uiautomator hierarchy
    ScreenshotProvider    pixels, only when a level that needs them runs
    OCRProvider           text lines with boxes, in screenshot pixels
    VisionProvider        a vision model's elements for a screenshot (optionally for one query)
    UIElementDetector     any detector of UI elements in pixels (a local model, a service)

Every source reports what it can do (``capability()``), and a source that cannot run is skipped
and recorded in the state's ``perception`` list. Nothing is invented. Sources only *read*: the
engine that drives them runs inside the ``computer.state`` action, so a screen capture is
classified, policy-checked and audited like any other action.

These interfaces replace nothing in :mod:`highhx.computer.providers`. Those are the act-capable
providers of the observe → act runtime; these are the read-only inputs of a fused
:class:`~highhx.perception.state.ComputerState`.
"""

from __future__ import annotations

import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from highhx.computer.providers import Capability
from highhx.perception.state import (
    BrowserState,
    DeviceInfo,
    PerceptionRecord,
    ScreenshotRef,
    StateElement,
    TabInfo,
    WindowInfo,
)

if TYPE_CHECKING:
    from highhx.computer.browser import ChromeBrowser
    from highhx.computer.driver import HighhXDriver
    from highhx.execution.cancellation import CancellationToken
    from highhx.models.interfaces import VisionModel


@dataclass
class Perceived:
    """The working set of one observation, filled by the sources and frozen by
    :class:`~highhx.perception.fusion.StateFusion`."""

    surface: str
    structured: list[StateElement] = field(default_factory=list)
    ocr: list[StateElement] = field(default_factory=list)
    """Text lines in screenshot pixels."""
    vision: list[StateElement] = field(default_factory=list)
    """Detected elements in screenshot pixels."""
    screenshot: ScreenshotRef | None = None
    device: DeviceInfo = field(default_factory=DeviceInfo)
    active_app: str = ""
    active_window: WindowInfo | None = None
    windows: list[WindowInfo] = field(default_factory=list)
    processes: list[str] = field(default_factory=list)
    cursor: tuple[int, int] | None = None
    viewport: tuple[int, int] | None = None
    browser: BrowserState | None = None
    text: str = ""
    records: list[PerceptionRecord] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class StructureProvider(Protocol):
    name: str
    source: str
    """dom · ax · android"""

    def capability(self) -> Capability: ...

    def perceive(self, into: Perceived, *, cancel: CancellationToken | None = None) -> None: ...


class DOMProvider(StructureProvider, Protocol):
    """A browser page's structure (roles, accessible names, attributes, bounds)."""


class AccessibilityProvider(StructureProvider, Protocol):
    """A native accessibility tree (macOS AX, Windows UI Automation, Linux AT-SPI, Android)."""


@runtime_checkable
class ScreenshotProvider(Protocol):
    name: str

    def capability(self) -> Capability: ...

    def capture(self, *, cancel: CancellationToken | None = None) -> ScreenshotRef: ...


@runtime_checkable
class OCRProvider(Protocol):
    name: str

    def capability(self) -> Capability: ...

    def read(self, shot: ScreenshotRef, *, cancel: CancellationToken | None = None) -> list[StateElement]: ...


@runtime_checkable
class VisionProvider(Protocol):
    name: str

    def capability(self) -> Capability: ...

    def detect(
        self, shot: ScreenshotRef, *, query: str | None = None, cancel: CancellationToken | None = None
    ) -> list[StateElement]: ...


UIElementDetector = VisionProvider
"""A detector has the vision provider's shape: a screenshot in, elements with boxes out."""


def shot_file(shot: ScreenshotRef) -> tuple[Path, bool]:
    """A file holding ``shot`` (and whether it is a temporary one the caller must delete)."""
    if shot.path and Path(shot.path).is_file():
        return Path(shot.path), False
    if shot.data is None:
        raise ValueError("the screenshot has neither a file nor its pixels")
    with tempfile.NamedTemporaryFile(prefix="highhx-shot-", suffix=".png", delete=False) as handle:
        handle.write(shot.data)
    return Path(handle.name), True


# --------------------------------------------------------------------- desktop
class DesktopAccessibility:
    """The desktop through the HighhX Computer API: frontmost app and window, windows, the
    accessibility tree with bounds, and the pointer."""

    name = "desktop-accessibility"
    source = "ax"

    def __init__(self, driver: HighhXDriver, *, app: str | None = None, limit: int = 300) -> None:
        self.driver = driver
        self.app = app
        self.limit = limit

    def capability(self) -> Capability:
        features = self.driver.capabilities()
        tree = features.get("accessibility")
        if tree is None:
            return Capability(self.name, True, "accessibility support not reported; trying")
        return Capability(self.name, tree.available, tree.detail)

    def perceive(self, into: Perceived, *, cancel: CancellationToken | None = None) -> None:
        from highhx.automation.engine.bridge import EngineError

        front, title, window = self.driver.active()
        into.active_app = front
        windows = self.driver.windows()
        into.windows = [
            WindowInfo(str(w.id), w.app, w.title, (w.x, w.y, w.width, w.height), window is not None and w.id == window.id)
            for w in windows
        ]
        if window is not None:
            into.active_window = WindowInfo(
                str(window.id), window.app, window.title or title, (window.x, window.y, window.width, window.height), True
            )
        elif front:
            into.active_window = WindowInfo("", front, title, None, True)
        into.processes = sorted({w.app for w in windows if w.app})
        try:
            into.cursor = self.driver.cursor()
        except EngineError:
            into.cursor = None
        tree = self.driver.get_ui_tree(self.app, limit=self.limit)
        into.structured += [StateElement.from_ui(e, source="ax") for e in tree.elements]
        if tree.text and not into.text:
            into.text = tree.text
        try:
            screen = self.driver.screen()
            into.device = DeviceInfo(sys.platform, "", "", (screen.width, screen.height), screen.scale)
        except EngineError:
            into.device = DeviceInfo(sys.platform)


class DesktopScreenshots:
    name = "desktop-screenshot"

    def __init__(self, driver: HighhXDriver, *, max_size: int | None = None) -> None:
        self.driver = driver
        self.max_size = max_size

    def capability(self) -> Capability:
        feature = self.driver.capabilities().get("screenshot")
        if feature is None:
            return Capability(self.name, True, "screenshots not reported; trying")
        return Capability(self.name, feature.available, feature.detail)

    def capture(self, *, cancel: CancellationToken | None = None) -> ScreenshotRef:
        shot = self.driver.screenshot(max_size=self.max_size)
        data = shot.path.read_bytes()
        return ScreenshotRef.from_bytes(data, scale=shot.scale or 1.0, path=str(shot.path))


# --------------------------------------------------------------------- browser
class BrowserDOM:
    """The HighhX browser's page: structure, URL, title, tabs and loading state."""

    name = "browser-dom"
    source = "dom"

    def __init__(self, browser: ChromeBrowser) -> None:
        self.browser = browser

    def capability(self) -> Capability:
        return self.browser.capability()

    def perceive(self, into: Perceived, *, cancel: CancellationToken | None = None) -> None:
        observation = self.browser.observe(cancel=cancel)
        into.structured += [StateElement.from_ui(e, source="dom") for e in observation.elements]
        into.active_app = observation.application or "browser"
        into.text = observation.text
        tabs: list[TabInfo] = []
        lister = getattr(self.browser, "list_tabs", None)
        if callable(lister):
            try:
                tabs = [
                    TabInfo(str(t.get("id", "")), str(t.get("url", "")), str(t.get("title", "")), bool(t.get("active")))
                    for t in lister(cancel=cancel) or []
                ]
            except Exception:  # tabs are optional context; the page itself was observed
                tabs = []
        loading = getattr(self.browser, "last_wait_settled", True) is False
        into.browser = BrowserState(observation.url, observation.title, tuple(tabs), loading)
        into.device = DeviceInfo(sys.platform)


class BrowserScreenshots:
    name = "browser-screenshot"

    def __init__(self, browser: ChromeBrowser) -> None:
        self.browser = browser

    def capability(self) -> Capability:
        return self.browser.capability()

    def capture(self, *, cancel: CancellationToken | None = None) -> ScreenshotRef:
        """The visible viewport, one image pixel per CSS pixel. That is the space browser input and
        DOM bounds use, so a box found in the image can be clicked as it is (a plain
        screenshot is in device pixels: twice as large on a HiDPI display)."""
        capture = getattr(self.browser, "page_capture", None)
        if callable(capture):
            data, _view = capture(cancel=cancel)
            return ScreenshotRef.from_bytes(data, scale=1.0)
        return ScreenshotRef.from_bytes(self.browser.screenshot(cancel=cancel))


# ------------------------------------------------------------------------- OCR
class TesseractOCRProvider:
    """Local OCR with tesseract (when installed). Lines come back in screenshot pixels."""

    name = "tesseract"

    def __init__(self) -> None:
        from highhx.computer.desktop import TesseractOCR

        self.reader = TesseractOCR()

    def capability(self) -> Capability:
        return self.reader.capability()

    def read(self, shot: ScreenshotRef, *, cancel: CancellationToken | None = None) -> list[StateElement]:
        path, temporary = shot_file(shot)
        try:
            observation = self.reader.read_image(path, cancel=cancel)
        finally:
            if temporary:
                path.unlink(missing_ok=True)
        out = []
        for e in observation.elements:
            confidence = e.attributes.get("confidence") or e.attributes.get("conf") or "90"
            try:
                score = max(0.0, min(1.0, float(confidence) / 100))
            except ValueError:
                score = 0.9
            out.append(StateElement.from_ui(e, source="ocr", confidence=score))
        return out


def record(source: str, status: str, detail: str = "", started: float | None = None, count: int = 0) -> PerceptionRecord:
    return PerceptionRecord(source, status, detail, time.monotonic() - started if started else 0.0, count)


# ---------------------------------------------------------------------- vision
class ModelVisionProvider:
    """A :class:`~highhx.models.interfaces.VisionModel` as a perception source. With a query it
    asks where that one thing is; without one it asks for the visible interactive elements.
    Boxes stay in screenshot pixels (fusion converts them to points)."""

    def __init__(self, model: VisionModel | None) -> None:
        self.model = model
        self.name = f"vision:{model.name}" if model is not None else "vision"

    def capability(self) -> Capability:
        if self.model is None:
            return Capability(self.name, False, "no vision model is configured (computer.vision / HIGHHX_VISION_*)")
        where = "local" if getattr(self.model, "local", False) else "remote — screenshots leave this computer"
        return Capability(self.name, True, f"{self.model.name} ({where})")

    def detect(
        self, shot: ScreenshotRef, *, query: str | None = None, cancel: CancellationToken | None = None
    ) -> list[StateElement]:
        if self.model is None:
            return []
        if shot.data is None:
            path, temporary = shot_file(shot)
            data = path.read_bytes()
            if temporary:
                path.unlink(missing_ok=True)
        else:
            data = shot.data
        found = self.model.locate(data, query, cancel=cancel) if query else self.model.detect(data, cancel=cancel)
        return [
            StateElement(
                f"v{i}",
                item.role or "image",
                item.label,
                bounds=item.box,
                sources=("vision",),
                confidence=item.confidence,
            )
            for i, item in enumerate(found, 1)
        ]
