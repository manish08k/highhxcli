"""ComputerState, PNG decoding, fusion, visual/state diffs, element tracking and the perception
engine's escalation (structure → OCR → vision) — on deterministic fixtures."""

from __future__ import annotations

import dataclasses

import pytest

from highhx.core.errors import IntegrationError
from highhx.perception import (
    ComputerState,
    ElementTracker,
    PerceptionEngine,
    PerceptionPolicy,
    StateDiff,
    StateElement,
    VisualDiff,
)
from highhx.perception.png import PNGError, crop, decode, encode, png_size
from highhx.perception.state import ScreenshotRef, overlap
from tests.unit.perception.fakes import FakeOCR, FakeScreens, FakeStructure, FakeVision, el, png


# ------------------------------------------------------------------ state
def test_state_is_immutable_and_round_trips() -> None:
    state = ComputerState(
        "browser",
        elements=(el("e1", "button", "Save", (10, 10, 40, 20)), el("e2", "textbox", "Email", attrs={"type": "email"})),
        text="Save Email",
        screenshot=ScreenshotRef.from_bytes(png()),
        metadata=(("k", "v"),),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.text = "x"  # type: ignore[misc]
    again = ComputerState.from_dict(state.to_dict())
    assert again.fingerprint() == state.fingerprint()
    assert again.elements == state.elements and again.meta("k") == "v"
    assert again.screenshot is not None and again.screenshot.data is None  # pixels are never serialized
    assert state.with_(text="other").text == "other" and state.text == "Save Email"


def test_unknown_surface_is_refused() -> None:
    with pytest.raises(ValueError):
        ComputerState("tv")


def test_secret_values_are_never_kept() -> None:
    secret = StateElement("p", "textbox", "Password", value="hunter2", attributes=(("type", "password"),))
    assert secret.to_dict()["value"] == ""
    from highhx.computer.model import UIElement

    ui = UIElement("p", "textbox", "Password", value="hunter2", attributes={"type": "password"})
    assert StateElement.from_ui(ui).value == ""


def test_lookups() -> None:
    state = ComputerState(
        "desktop",
        elements=(el("w", "window", "Main", (0, 0, 500, 500)), el("b", "button", "Save draft", (10, 10, 50, 20))),
    )
    assert [e.id for e in state.find(role="button", name="save")] == ["b"]
    assert state.find(name="save", exact=True) == []
    assert state.at((20, 15)).id == "b" and state.at((300, 300)).id == "w" and state.at((900, 900)) is None


def test_observation_conversion_keeps_structure() -> None:
    from highhx.computer.model import Observation, UIElement

    obs = Observation("chrome", "Chrome", "Shop", "https://x/", [UIElement("e1", "link", "Docs", bounds=(1, 2, 3, 4))])
    state = ComputerState.from_observation(obs)
    assert state.url == "https://x/" and state.title == "Shop" and state.elements[0].bounds == (1, 2, 3, 4)
    back = state.to_observation()
    assert back.elements[0].name == "Docs" and back.url == "https://x/"


# -------------------------------------------------------------------- png
def test_png_round_trip_and_crop() -> None:
    data = png(30, 20, ((5, 5, 4, 4),))
    assert png_size(data) == (30, 20)
    image = decode(data)
    assert image.pixel(6, 6) == (0, 0, 0) and image.pixel(0, 0) == (255, 255, 255)
    part = crop(image, 4, 4, 6, 6)
    assert part.width == 6 and part.pixel(2, 2) == (0, 0, 0)
    assert decode(encode(part.width, part.height, part.rgb)).rgb == part.rgb
    with pytest.raises(PNGError):
        png_size(b"GIF89a")


def test_png_decodes_every_filter_type() -> None:
    import struct
    import zlib

    width, height = 4, 5
    rows = [bytes([10 * (x + y) % 256 for x in range(width) for _ in range(3)]) for y in range(height)]
    raw = b""
    prev = bytes(width * 3)
    for kind, row in enumerate(rows):
        filtered = bytearray()
        for i, value in enumerate(row):
            left = row[i - 3] if i >= 3 else 0
            up = prev[i]
            corner = prev[i - 3] if i >= 3 else 0
            pred = [0, left, up, (left + up) >> 1, _paeth(left, up, corner)][kind]
            filtered.append((value - pred) & 0xFF)
        raw += bytes([kind]) + bytes(filtered)
        prev = row

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    assert decode(data).rgb == b"".join(rows)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


# ------------------------------------------------------------------- diff
def test_visual_diff_finds_where_the_screen_changed() -> None:
    diff = VisualDiff(cell=8)
    same = diff.compare(png(), png())
    assert not same.changed and same.ratio == 0
    changed = diff.compare(png(), png(boxes=((40, 24, 16, 16),)))
    assert changed.changed and changed.method == "pixels"
    (x, y, w, h) = changed.regions[0]
    assert x <= 40 and y <= 24 and x + w >= 56 and y + h >= 40
    resized = diff.compare(png(80, 60), png(60, 40))
    assert resized.changed and "size" in resized.detail


def test_visual_diff_ignores_noise_below_the_threshold() -> None:
    from highhx.perception.png import solid

    base = solid(64, 64)
    noisy = bytearray(base)
    noisy[0:3] = bytes((250, 250, 250))  # one pixel, slightly different
    assert not VisualDiff(cell=8).compare(encode(64, 64, bytes(base)), encode(64, 64, bytes(noisy))).changed


def test_visual_diff_without_pixels_compares_digests() -> None:
    a = ScreenshotRef(10, 10, sha256="aaa")
    b = ScreenshotRef(10, 10, sha256="aaa")
    c = ScreenshotRef(10, 10, sha256="bbb")
    assert VisualDiff().compare(a, b).method == "digest" and not VisualDiff().compare(a, b).changed
    assert VisualDiff().compare(a, c).changed


def test_state_diff_reports_structural_changes() -> None:
    before = ComputerState(
        "browser",
        browser=None,
        elements=(el("e1", "button", "Submit", (0, 0, 10, 10)), el("e2", "checkbox", "Agree", checked=False)),
        text="Form",
    )
    after = ComputerState(
        "browser",
        elements=(el("e9", "checkbox", "Agree", checked=True), el("e10", "text", "Thanks", (0, 0, 10, 10))),
        text="Form\nThanks",
    )
    diff = StateDiff.between(before, after)
    assert diff.changed and 'button "Submit"' in diff.disappeared and 'text "Thanks"' in diff.appeared
    assert 'checkbox "Agree"' in diff.modified and diff.text_added == "Thanks"


# ---------------------------------------------------------------- tracker
def test_tracker_follows_controls_across_new_ids() -> None:
    tracker = ElementTracker()
    first = ComputerState("browser", elements=(el("e1", "button", "Delete", (0, 0, 10, 10)), el("e2", "button", "Delete", (0, 20, 10, 10))))
    assert not tracker.update(first).changed
    track = tracker.track_of("e2")
    second = ComputerState("browser", elements=(el("x7", "button", "Delete", (0, 0, 10, 10)), el("x8", "button", "Delete", (0, 50, 10, 10))))
    changes = tracker.update(second)
    assert tracker.track_of("x8") == track and [e.id for e in changes.moved] == ["x8"]
    third = ComputerState("browser", elements=(el("y1", "button", "Delete", (0, 0, 10, 10)),))
    assert [e.id for e in tracker.update(third).disappeared] == ["x8"]


def test_overlap() -> None:
    assert overlap((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert overlap((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0
    assert 0 < overlap((0, 0, 10, 10), (5, 5, 10, 10)) < 1


# ------------------------------------------------------------------ fusion
def test_fusion_merges_trees_and_corroborates_with_ocr() -> None:
    dom = FakeStructure([el("e1", "button", "Submit", (100, 100, 80, 30))])
    ax = FakeStructure([el("a1", "button", "Submit", (101, 100, 80, 30), source="ax")], source="ax")
    ocr = FakeOCR(
        [
            el("o1", "text", "Submit", (220, 210, 120, 40), source="ocr"),  # pixels at scale 2 → inside the button
            el("o2", "text", "Canvas label", (600, 400, 200, 40), source="ocr", confidence=0.7),
        ]
    )
    engine = PerceptionEngine("browser", structure=[dom, ax], screenshot=FakeScreens([png()], scale=2.0), ocr=ocr)
    state = engine.observe(policy=PerceptionPolicy(ocr="always"))
    submit = state.find(role="button", name="Submit")
    assert len(submit) == 1 and set(submit[0].sources) == {"dom", "ax", "ocr"}
    (label,) = state.find(name="Canvas label")
    assert label.sources == ("ocr",) and label.bounds == (300, 200, 100, 20) and label.confidence == 0.7
    assert "Canvas label" in state.ocr_text


# ------------------------------------------------------------------ engine
def test_engine_escalates_only_when_needed() -> None:
    dom = FakeStructure([el("e1", "button", "Save", (0, 0, 10, 10))])
    screens = FakeScreens([png()])
    ocr = FakeOCR([el("o1", "text", "Export PDF", (0, 30, 50, 10), source="ocr")])
    vision = FakeVision([el("v1", "button", "Gear icon", (60, 30, 10, 10), source="vision", confidence=0.9)])
    engine = PerceptionEngine("browser", structure=[dom], screenshot=screens, ocr=ocr, vision=vision)
    state = engine.observe(query="Save")
    assert screens.taken == 0 and ocr.calls == 0 and vision.queries == [] and state.screenshot is None
    engine.invalidate()
    state = engine.observe(query="Export PDF")
    assert screens.taken == 1 and ocr.calls == 1 and vision.queries == []  # OCR found it: no model
    engine.invalidate()
    state = engine.observe(query="Gear icon", policy=PerceptionPolicy(vision="auto"))
    assert vision.queries == ["Gear icon"] and state.find(name="Gear icon")[0].sources == ("vision",)
    engine.invalidate()
    engine.observe(query="Nowhere")  # vision 'never' by default: a model never sees the screen unasked
    assert vision.queries == ["Gear icon"]


def test_engine_caches_until_invalidated() -> None:
    dom = FakeStructure([el("e1", "button", "Save")])
    clock = [0.0]
    engine = PerceptionEngine("browser", structure=[dom], clock=lambda: clock[0])
    engine.observe()
    engine.observe()
    assert dom.calls == 1
    clock[0] = 10.0
    engine.observe()
    assert dom.calls == 2
    engine.invalidate()
    engine.observe()
    assert dom.calls == 3


def test_unavailable_and_failing_sources_are_recorded_never_faked() -> None:
    dom = FakeStructure([], fail=IntegrationError("browser crashed"))
    ocr = FakeOCR([], available=False)
    events: list[tuple[str, dict]] = []
    engine = PerceptionEngine(
        "browser", structure=[dom], screenshot=FakeScreens([png()]), ocr=ocr, emit=lambda n, **d: events.append((n, d))
    )
    state = engine.observe()
    records = {r.source: r for r in state.perception}
    assert records["dom"].status == "failed" and "crashed" in records["dom"].detail
    assert records["ocr"].status == "unavailable" and "tesseract" in records["ocr"].detail
    assert state.elements == () and events[0][0] == "observation.created"


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        PerceptionPolicy(ocr="sometimes")
