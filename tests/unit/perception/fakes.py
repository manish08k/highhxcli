"""Deterministic perception sources for tests: a structure tree, screenshots, OCR and vision that
return fixed data — nothing reads a real screen."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from highhx.computer.providers import Capability
from highhx.perception.png import encode, fill, solid
from highhx.perception.providers import Perceived
from highhx.perception.state import BrowserState, ScreenshotRef, StateElement


def png(width: int = 80, height: int = 60, boxes: tuple[tuple[int, int, int, int], ...] = ()) -> bytes:
    rgb = solid(width, height)
    for box in boxes:
        fill(rgb, width, box, (0, 0, 0))
    return encode(width, height, bytes(rgb))


@dataclass
class FakeStructure:
    elements: list[StateElement]
    source: str = "dom"
    name: str = "fake-structure"
    available: bool = True
    url: str = "https://shop.test/"
    title: str = "Shop"
    text: str = ""
    calls: int = 0
    fail: Exception | None = None

    def capability(self) -> Capability:
        return Capability(self.name, self.available, "fake")

    def perceive(self, into: Perceived, *, cancel: Any = None) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        into.structured += list(self.elements)
        into.active_app = "Chrome"
        into.text = self.text or " ".join(e.name for e in self.elements)
        if self.source == "dom":
            into.browser = BrowserState(self.url, self.title)


@dataclass
class FakeScreens:
    shots: list[bytes]
    scale: float = 1.0
    name: str = "fake-screens"
    available: bool = True
    taken: int = 0

    def capability(self) -> Capability:
        return Capability(self.name, self.available, "fake")

    def capture(self, *, cancel: Any = None) -> ScreenshotRef:
        data = self.shots[min(self.taken, len(self.shots) - 1)]
        self.taken += 1
        return ScreenshotRef.from_bytes(data, scale=self.scale)


@dataclass
class FakeOCR:
    lines: list[StateElement]
    name: str = "fake-ocr"
    available: bool = True
    calls: int = 0

    def capability(self) -> Capability:
        return Capability(self.name, self.available, "fake ocr" if self.available else "install tesseract")

    def read(self, shot: ScreenshotRef, *, cancel: Any = None) -> list[StateElement]:
        self.calls += 1
        return list(self.lines)


@dataclass
class FakeVision:
    elements: list[StateElement]
    name: str = "fake-vision"
    available: bool = True
    queries: list[str | None] = field(default_factory=list)

    def capability(self) -> Capability:
        return Capability(self.name, self.available, "fake vision")

    def detect(self, shot: ScreenshotRef, *, query: str | None = None, cancel: Any = None) -> list[StateElement]:
        self.queries.append(query)
        return list(self.elements)


def el(eid: str, role: str, name: str, bounds: tuple[int, int, int, int] | None = None, **kw: Any) -> StateElement:
    source = kw.pop("source", "dom")
    attributes = tuple(sorted((k, str(v)) for k, v in kw.pop("attrs", {}).items()))
    return StateElement(eid, role, name, bounds=bounds, sources=(source,), attributes=attributes, **kw)
