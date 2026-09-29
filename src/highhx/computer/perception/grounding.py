"""Grounding a piece of text to a desktop point.

Structured first: an accessibility element whose name matches gives exact bounds, no pixels
needed. Only when the accessibility tree has no match (or is unavailable) is the screen read
with OCR — and OCR works in screenshot *pixels*, so its boxes are divided by the display's
scale factor (2.0 on Retina) to become desktop *points*, the space every input operation uses.

Matching is exact before partial, case-insensitive; several equally good matches are an error
listing them, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from highhx.automation.engine.bridge import EngineError
from highhx.computer.model import UIElement
from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver


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

    def to_dict(self) -> dict[str, Any]:
        return {"point": list(self.point), "source": self.source, "text": self.text, "bounds": list(self.bounds)}


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
        return Grounded(_center(element.bounds), "accessibility", element.name, element.bounds)
    if not reasons:
        reasons.append("accessibility: no element with that name")
    if ocr:
        found = _ocr(driver, text, reasons)
        if found is not None:
            return found
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
    return Grounded(_center(line.bounds, scale), "ocr", line.name, bounds)
