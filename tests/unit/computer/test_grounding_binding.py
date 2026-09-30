"""Capture/action binding for perception: a grounded point belongs to the window it was found in,
as that window was when the screen was read. A window that moves, closes or is covered before the
click makes the point stale — the click is refused, never sent somewhere else."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer import HighhXDriver
from highhx.computer.model import Observation, UIElement
from highhx.computer.perception import ground, still_there
from tests.unit.automation.fakes import FakeEngine

NOTES = {"id": 7, "pid": 70, "app": "Notes", "title": "Untitled", "x": 0, "y": 25, "width": 800, "height": 600}


@pytest.fixture
def desktop() -> tuple[HighhXDriver, FakeEngine]:
    fake = FakeEngine()
    return HighhXDriver(AutomationBridge(fake)), fake


def test_a_grounded_point_is_bound_to_the_window_under_it(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, _fake = desktop
    found = ground(driver, "save", ocr=False)
    assert found.window is not None and found.window.id == 7 and found.to_dict()["window"] == 7
    assert still_there(driver, found) is None


def test_moved_closed_or_covered_windows_make_the_point_stale(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, fake = desktop
    found = ground(driver, "save", ocr=False)
    fake.windows = [{**NOTES, "x": 300}]
    assert "moved" in (still_there(driver, found) or "")
    fake.windows = [{**NOTES, "x": 2}]  # within the slack a window manager may apply
    assert still_there(driver, found) is None
    fake.windows = [{**NOTES, "id": 9, "app": "Mail"}, NOTES]  # another window on top
    assert "covered" in (still_there(driver, found) or "")
    fake.windows = []
    assert "closed" in (still_there(driver, found) or "")


def test_ocr_binds_to_the_desktop_as_captured(
    desktop: tuple[HighhXDriver, FakeEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.computer import desktop as desktop_module
    from highhx.computer.providers import Capability

    driver, fake = desktop
    fake.elements = []
    monkeypatch.setattr(desktop_module.TesseractOCR, "capability", lambda self: Capability("ocr", True, "fake"))

    def read_image(self: Any, image: Path, **_kw: Any) -> Observation:
        fake.windows = [{**NOTES, "y": 400}]  # the window moves while OCR is still reading the capture
        return Observation("ocr", "screen", elements=[UIElement("o1", "text", "Export", bounds=(200, 400, 100, 20))])

    monkeypatch.setattr(desktop_module.TesseractOCR, "read_image", read_image)
    found = ground(driver, "export")
    assert found.window is not None and found.window.y == 25  # bound to what was captured …
    assert "moved" in (still_there(driver, found) or "")  # … so the move is caught
