"""Page screenshots and grounding: an image pixel is the point pointer input takes, also under
pinch zoom; a capture is stale once the page scrolls, resizes, navigates, zooms or pans.
Real Chrome: test_live_browser.py::test_screenshot_pixels_are_click_points_… (HIGHHX_TEST_BROWSER=1)."""

from __future__ import annotations

from typing import Any

import pytest

from highhx.computer.capture import CaptureStore, StaleCapture
from highhx.perception.png import encode, solid


class PinchZoomedPage:
    """A 1280x800 layout viewport pinch-zoomed 2x: 640x400 CSS pixels of it are visible."""

    def __init__(self) -> None:
        self.visual = {"x": 1184.0, "y": 1001.0, "width": 640.0, "height": 400.0, "scale": 2.0}

    def viewport(self, *, cancel: Any = None) -> dict[str, Any]:
        return {
            "url": "http://127.0.0.1/p",
            "title": "P",
            "x": 544,
            "y": 594,
            "width": 1280,
            "height": 800,
            "dpr": 2,
            "visual": dict(self.visual),
        }

    def page_capture(self, *, cancel: Any = None) -> tuple[bytes, dict[str, Any]]:
        width, height = int(self.visual["width"]), int(self.visual["height"])
        return encode(width, height, bytes(solid(width, height))), self.viewport()


def test_a_pinch_zoomed_page_screenshot_grounds_pixel_for_point() -> None:
    page = PinchZoomedPage()
    store = CaptureStore()
    capture = store.take_page(page)
    # Regression: the scale was image width / layout width (0.5), doubling every point
    assert capture.shot.scale == 1.0
    assert store.ground_page(page, capture.id, 100, 50) == (100, 50)
    assert store.ground_page(page, capture.id, 500, 500, space="relative1000") == (320, 200)


def test_zooming_or_panning_after_the_screenshot_makes_it_stale() -> None:
    page = PinchZoomedPage()
    store = CaptureStore()
    capture = store.take_page(page)
    page.visual["x"] += 40  # panned: the same pixel now shows another part of the page
    with pytest.raises(StaleCapture, match="zoomed or panned"):
        store.ground_page(page, capture.id, 100, 50)
