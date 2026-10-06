"""Screenshot redaction: secret fields (password, card, one-time code; desktop secure fields) are
blacked out — mapped exactly from their own space into the image's pixels. Real Chrome:
test_live_browser.py::test_screenshots_black_out_secret_fields (HIGHHX_TEST_BROWSER=1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from highhx.automation.engine.bridge import AutomationBridge
from highhx.computer.driver import HighhXDriver, Screenshot
from highhx.computer.model import Observation, UIElement
from highhx.perception.png import decode, encode, solid
from highhx.perception.redaction import PAD, redact_png, scaled
from tests.computer_use.environment import SimulatedDesktop

WHITE = (255, 255, 255)


def white(width: int, height: int) -> bytes:
    return encode(width, height, bytes(solid(width, height, WHITE)))


def test_boxes_are_filled_with_a_margin_and_clipped_to_the_image() -> None:
    data, count = redact_png(white(40, 30), [(10, 10, 5, 4), (35, 25, 20, 20), (100, 100, 5, 5)])
    image = decode(data)
    assert count == 2  # the third box is outside the image
    assert image.pixel(10, 10) == (0, 0, 0) and image.pixel(10 - PAD, 10 - PAD) == (0, 0, 0)
    assert image.pixel(10 - PAD - 1, 10) == WHITE and image.pixel(16 + PAD, 10) == WHITE
    assert image.pixel(39, 29) == (0, 0, 0)  # clipped, not an error
    untouched = white(10, 10)
    assert redact_png(untouched, []) == (untouched, 0) and redact_png(untouched, [(50, 50, 2, 2)])[1] == 0


def test_boxes_are_scaled_and_offset_outward() -> None:
    assert scaled([(10.4, 5.0, 20.2, 8.0)], 2.0) == [(20, 10, 42, 16)]  # 20.8..61.2 rounded outward
    assert scaled([(110, 60, 10, 10)], 2.0, 100, 50) == [(20, 20, 20, 20)]  # a window at (100, 50)


def test_desktop_screenshots_black_out_secure_fields(tmp_path: Path, monkeypatch: Any) -> None:
    desktop = Path(__file__).resolve().parents[2] / "computer_use" / "tasks" / "_desktop.yaml"
    driver = HighhXDriver(AutomationBridge(SimulatedDesktop(yaml.safe_load(desktop.read_text()))))
    secure = UIElement("a1", "textbox", "Password", attributes={"type": "password"}, bounds=(110, 60, 10, 5))
    plain = UIElement("a2", "textbox", "Name", bounds=(110, 80, 10, 5))
    monkeypatch.setattr(
        driver, "get_ui_tree", lambda app=None, **_k: Observation("app", "App", "", "", [secure, plain])
    )
    path = tmp_path / "shot.png"
    path.write_bytes(white(100, 100))
    shot = driver._redact(Screenshot(path, 100, 100, scale=2.0, origin=(100, 50)))  # a window, 2x display
    image = decode(path.read_bytes())
    assert shot.redacted == 1
    assert image.pixel(20, 20) == (0, 0, 0) and image.pixel(39, 29) == (0, 0, 0)  # (110,60)+(10,5) at 2x
    assert image.pixel(20, 61) == WHITE  # the plain field is untouched
