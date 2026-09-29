"""The HighhX Computer API over a simulated desktop: typed results, sessions, observation,
and perception (accessibility first, OCR as the fallback, never a guess)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.computer import HighhXDriver
from highhx.computer.driver import menu_path, parse_hotkey
from highhx.computer.model import UIElement
from highhx.computer.perception import GroundingError, ground
from highhx.computer.perception.grounding import best
from highhx.core.errors import UsageError
from highhx.utils.paths import user_data_dir
from tests.unit.automation.fakes import FakeEngine


@pytest.fixture
def desktop() -> tuple[HighhXDriver, FakeEngine]:
    fake = FakeEngine()
    return HighhXDriver(AutomationBridge(fake)), fake


def test_typed_observation(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, _fake = desktop
    assert driver.screen().scale == 2.0
    assert [w.id for w in driver.windows()] == [7] and driver.windows()[0].center == (400, 325)
    tree = driver.get_ui_tree()
    assert [(e.name, e.bounds) for e in tree.elements] == [("Save", (100, 100, 80, 30)), ("Delete", (200, 100, 80, 30))]
    element = driver.element_at(110, 110)
    assert element.name == "Save" and element.attributes["app"] == "Notes"
    state = driver.observe(screenshot=True)
    assert state.app == "Notes" and state.tree is not None and state.screenshot is not None
    assert state.screenshot.path.is_file() and state.screenshot.path.parent == user_data_dir() / "screenshots"
    assert state.to_dict()["elements"][0]["bounds"] == [100, 100, 80, 30]


def test_observation_leaves_out_what_the_platform_cannot_give(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, fake = desktop
    fake.deny = "inspect"
    state = driver.observe()
    assert state.tree is None and state.windows  # the rest is still observed; nothing is invented


def test_input_and_sessions(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, fake = desktop
    driver.double_click(110, 110)
    driver.right_click(5, 6)
    driver.drag((1, 2), (3, 4))
    driver.hotkey("cmd+shift+s")
    driver.hotkey("enter")
    driver.invoke_menu("Notes", "File > Save")
    assert fake.sent("click_at") == [
        ("click_at", {"x": 110, "y": 110, "button": "left", "count": 2}),
        ("click_at", {"x": 5, "y": 6, "button": "right", "count": 1}),
    ]
    assert fake.sent("hotkey") == [("hotkey", {"modifiers": ["command", "shift"], "key": "s"})]
    assert fake.sent("key") == [("key", {"key": "enter"})] and fake.chosen == [["File", "Save"]]
    assert driver.session.actions == 6 and driver.session.last_action == "menu"
    driver.windows()  # reading does not count as acting
    assert driver.session.actions == 6
    assert HighhXDriver.get_session(driver.session.id) is driver.session
    assert driver.session in HighhXDriver.list_sessions()
    driver.end_session()
    assert HighhXDriver.get_session(driver.session.id) is None
    with pytest.raises(UsageError, match="ended"):
        driver.cursor()


def test_only_the_local_computer_exists_in_this_build() -> None:
    for target in ("cloud", "sandbox", "vm"):
        with pytest.raises(UsageError, match="local computer only"):
            HighhXDriver.create(target=target)


def test_capabilities_are_structured_even_when_the_engine_fails(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, fake = desktop
    assert all(f.available for f in driver.capabilities().values())
    fake.deny = "capabilities"
    assert not any(f.available for f in driver.capabilities().values())


def test_parsing_helpers() -> None:
    assert parse_hotkey("Cmd + Shift + T") == (["cmd", "shift"], "t")
    assert parse_hotkey(["ctrl", "c"]) == (["ctrl"], "c")
    with pytest.raises(UsageError, match="unknown modifier"):
        parse_hotkey("hyper+t")
    assert menu_path("File > Save As…") == ["File", "Save As…"]
    with pytest.raises(UsageError):
        menu_path(" > ")


# --------------------------------------------------------------- perception
def test_grounding_prefers_the_accessibility_tree(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, _fake = desktop
    found = ground(driver, "save", ocr=False)
    assert found.point == (140, 115) and found.source == "accessibility"


def test_grounding_never_guesses_between_matches() -> None:
    elements = [
        UIElement("a1", "button", "Save", bounds=(0, 0, 10, 10)),
        UIElement("a2", "button", "Save", bounds=(50, 0, 10, 10)),
    ]
    with pytest.raises(GroundingError, match="matches 2 places"):
        best(elements, "save")
    assert best([*elements, UIElement("a3", "button", "Save as", bounds=(0, 50, 10, 10))], "save as") is not None


def test_grounding_falls_back_to_ocr_in_points_not_pixels(
    desktop: tuple[HighhXDriver, FakeEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.computer import desktop as desktop_module
    from highhx.computer.model import Observation
    from highhx.computer.providers import Capability

    driver, fake = desktop
    fake.elements = []  # nothing accessible (a canvas)
    monkeypatch.setattr(desktop_module.TesseractOCR, "capability", lambda self: Capability("ocr", True, "fake"))
    read: list[Path] = []

    def read_image(self: Any, image: Path, **_kw: Any) -> Observation:
        read.append(image)
        return Observation(
            "ocr", "screen", elements=[UIElement("o1", "text", "Export PDF", bounds=(200, 400, 100, 20))]
        )

    monkeypatch.setattr(desktop_module.TesseractOCR, "read_image", read_image)
    found = ground(driver, "export pdf")
    assert found.source == "ocr" and found.point == (125, 205)  # pixel (250, 410) ÷ scale 2
    assert found.bounds == (100, 200, 50, 10) and not read[0].exists()  # the capture is removed after use
    monkeypatch.setattr(
        desktop_module.TesseractOCR, "capability", lambda self: Capability("ocr", False, "tesseract is not installed")
    )
    with pytest.raises(GroundingError, match="tesseract is not installed"):
        ground(driver, "export pdf")


def test_grounding_reports_why_when_accessibility_is_unavailable(desktop: tuple[HighhXDriver, FakeEngine]) -> None:
    driver, fake = desktop
    fake.deny = "inspect"
    with pytest.raises(GroundingError, match="accessibility: macOS has not granted"):
        ground(driver, "Save", ocr=False)
    assert isinstance(EngineError, type)
