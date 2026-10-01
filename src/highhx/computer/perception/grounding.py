"""Grounding a piece of text to a desktop point.

Structured first: an accessibility element whose name matches gives exact bounds, no pixels
needed. Only when the accessibility tree has no match (or is unavailable) is the screen read
with OCR — and OCR works in screenshot *pixels*, so its boxes are divided by the display's
scale factor (2.0 on Retina) to become desktop *points*, the space every input operation uses.

Matching is exact before partial, case-insensitive; several equally good matches are an error
listing them, never a guess.

A grounded point is bound to the window under it when it was found (its id and frame — Cua binds
a pixel click to the capture it came from). :func:`still_there` checks that binding right before
the click: a window that moved, closed or was covered in the meantime makes the point stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from highhx.automation.engine.bridge import EngineError
from highhx.computer.capture import FRAME_SLACK, window_at
from highhx.computer.model import UIElement
from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver, Window



class GroundingError(HighhXError):
    """No element or text matches — or several do."""


@dataclass(frozen=True)
class Grounded:
    point: tuple[int, int]
    """Desktop points."""
    source: str
    """accessibility | ocr"""
    text: str
    bounds: tuple[int, int, int, int]
    window: Window | None = None
    """The window under ``point`` when it was grounded (None when windows cannot be listed)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": list(self.point),
            "source": self.source,
            "text": self.text,
            "bounds": list(self.bounds),
            "window": self.window.id if self.window else None,
        }


def _windows(driver: HighhXDriver) -> list[Window] | None:
    try:
        return driver.windows()
    except EngineError:
        return None  # windows cannot be listed here: the point stays unbound (reported as window None)


def _bind(found: Grounded, windows: list[Window] | None) -> Grounded:
    window = window_at(windows, found.point) if windows is not None else None
    return Grounded(found.point, found.source, found.text, found.bounds, window)


def still_there(driver: HighhXDriver, found: Grounded) -> str | None:
    """Why ``found`` may no longer point at what was grounded — or None when its window is still
    the frontmost one at the point, where it was."""
    if found.window is None:
        return None
    was = found.window
    windows = driver.windows()
    now = window_at(windows, found.point)
    name = was.app or "the window"
    if now is not None and now.id != was.id:
        return f"{name} is covered by {now.app or 'another window'} at {list(found.point)}"
    current = next((w for w in windows if w.id == was.id), None)
    if current is None:
        return f"{name} closed after {found.text!r} was found"
    frames = zip(
        (current.x, current.y, current.width, current.height), (was.x, was.y, was.width, was.height), strict=True
    )
    if max(abs(a - b) for a, b in frames) > FRAME_SLACK:
        return f"{name} moved or resized after {found.text!r} was found"
    return None


def best(elements: list[UIElement], text: str) -> UIElement | None:
    """The one element named ``text`` (exactly, else as part of its name); None when there is none."""
    wanted = " ".join(text.lower().split())
    placed = [e for e in elements if e.bounds and e.name.strip()]
    for pool in (
        [e for e in placed if " ".join(e.name.lower().split()) == wanted],
        [e for e in placed if wanted in " ".join(e.name.lower().split())],
    ):
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            names = ", ".join(f"{e.role} {e.name!r} at {e.bounds[:2] if e.bounds else '?'}" for e in pool[:5])
            raise GroundingError(f"{text!r} matches {len(pool)} places ({names}); be more specific.")
    return None


def _center(bounds: tuple[int, int, int, int], scale: float = 1.0) -> tuple[int, int]:
    x, y, width, height = bounds
    return round((x + width / 2) / scale), round((y + height / 2) / scale)


def ground(driver: HighhXDriver, text: str, *, app: str | None = None, ocr: bool = True) -> Grounded:
    """Where ``text`` is on screen, in desktop points."""
    reasons: list[str] = []
    element: UIElement | None = None
    try:
        element = best(driver.get_ui_tree(app).elements, text)
    except EngineError as exc:
        reasons.append(f"accessibility: {exc.message}")
    if element is not None and element.bounds is not None:
        found = Grounded(_center(element.bounds), "accessibility", element.name, element.bounds)
        return _bind(found, _windows(driver))
    if not reasons:
        reasons.append("accessibility: no element with that name")
    if ocr:
        read = _ocr(driver, text, reasons)
        if read is not None:
            return read
    raise GroundingError(f"{text!r} is not on the screen ({'; '.join(reasons)}).")


def _ocr(driver: HighhXDriver, text: str, reasons: list[str]) -> Grounded | None:
    from highhx.computer.desktop import TesseractOCR

    reader = TesseractOCR()
    capability = reader.capability()
    if not capability.available:
        reasons.append(f"ocr: {capability.detail}")
        return None
    try:
        shot = driver.screenshot()
    except EngineError as exc:
        reasons.append(f"ocr: {exc.message}")
        return None
    windows = _windows(driver)  # the desktop as captured: the point is bound to it, not to later state
    try:
        line = best(reader.read_image(shot.path).elements, text)
    finally:
        shot.path.unlink(missing_ok=True)  # the capture was only needed for this
    if line is None or line.bounds is None:
        reasons.append("ocr: the text was not read on the screen")
        return None
    scale = shot.scale or 1.0
    x, y, width, height = line.bounds
    bounds = (round(x / scale), round(y / scale), round(width / scale), round(height / scale))
    return _bind(Grounded(_center(line.bounds, scale), "ocr", line.name, bounds), windows)
