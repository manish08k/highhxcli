"""Multimodal perception: DOM, accessibility, OCR, screenshots and vision → one ComputerState.

    from highhx.perception import PerceptionEngine, PerceptionPolicy
    engine = PerceptionEngine("desktop", structure=[DesktopAccessibility(driver)],
                              screenshot=DesktopScreenshots(driver), ocr=TesseractOCRProvider())
    state = engine.observe(policy=PerceptionPolicy(ocr="auto"), query="Export PDF")

Read-only: perception never acts. Inside HighhX it runs as the ``computer.state`` action, so a
screen capture is classified, policy-checked and audited. See docs/PERCEPTION.md.
"""

from highhx.perception.diff import StateDiff, VisualDiff, VisualDiffResult
from highhx.perception.engine import PerceptionEngine, PerceptionPolicy
from highhx.perception.fusion import StateFusion
from highhx.perception.providers import (
    AccessibilityProvider,
    DOMProvider,
    OCRProvider,
    Perceived,
    ScreenshotProvider,
    StructureProvider,
    UIElementDetector,
    VisionProvider,
)
from highhx.perception.state import (
    BrowserState,
    ComputerState,
    DeviceInfo,
    PerceptionRecord,
    ScreenshotRef,
    StateElement,
    TabInfo,
    WindowInfo,
)
from highhx.perception.tracker import ElementTracker

__all__ = [
    "AccessibilityProvider",
    "BrowserState",
    "ComputerState",
    "DOMProvider",
    "DeviceInfo",
    "ElementTracker",
    "OCRProvider",
    "Perceived",
    "PerceptionEngine",
    "PerceptionPolicy",
    "PerceptionRecord",
    "ScreenshotProvider",
    "ScreenshotRef",
    "StateDiff",
    "StateElement",
    "StateFusion",
    "StructureProvider",
    "TabInfo",
    "UIElementDetector",
    "VisionProvider",
    "VisualDiff",
    "VisualDiffResult",
    "WindowInfo",
]
