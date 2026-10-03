"""One grounder per representation. Each looks at a ComputerState and returns candidates with a
confidence (0 … 1). None of them acts, and none of them picks among equals.

    AccessibilityGrounder   role + accessible name in the structured trees (DOM / AX / Android)
    DOMGrounder             stable attributes: id, data-testid, name, href, type, class tokens
    TextGrounder            visible text, ignoring role, fuzzy for small rewordings
    OCRGrounder             text read from pixels (fuzzy: OCR is noisy)
    VisionGrounder          a VisionModel asked where the target is in the screenshot
    CoordinateGrounder      the recorded point, only in the same app/page and viewport
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from highhx.core.errors import HighhXError
from highhx.perception.state import Bounds, ComputerState, StateElement, center

if TYPE_CHECKING:
    from highhx.execution.cancellation import CancellationToken
    from highhx.grounding.selectors import Target
    from highhx.models.interfaces import VisionModel

STRUCTURED = frozenset({"dom", "ax", "android"})


@dataclass(frozen=True)
class Candidate:
    strategy: str
    confidence: float
    element: StateElement | None = None
    box: Bounds | None = None
    reason: str = ""
    score: float = 0.0

    @property
    def point(self) -> tuple[int, int] | None:
        if self.element is not None and self.element.bounds is not None:
            return center(self.element.bounds)
        return center(self.box) if self.box else None

    @property
    def key(self) -> str:
        """What this candidate refers to (to recognise agreement between strategies)."""
        if self.element is not None and self.element.id:
            return f"el:{self.element.id}"
        return f"box:{self.box}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "element": self.element.to_dict() if self.element else None,
            "box": list(self.box) if self.box else None,
            "point": list(self.point) if self.point else None,
            "reason": self.reason,
        }


@dataclass
class Found:
    candidates: list[Candidate] = field(default_factory=list)
    status: str = "ok"
    """ok · unavailable (the strategy cannot run here) · skipped (nothing to look for)"""
    detail: str = ""


class Grounder(Protocol):
    name: str

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found: ...


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85 * min(len(a), len(b)) / max(len(a), len(b)) + 0.1
    return difflib.SequenceMatcher(None, a, b).ratio()


def _pixels(state: ComputerState) -> bytes | None:
    """The state's screenshot bytes (in memory, or from the capture file it points to)."""
    shot = state.screenshot
    if shot is None:
        return None
    if shot.data is not None:
        return shot.data
    if shot.path:
        from pathlib import Path

        try:
            return Path(shot.path).read_bytes()
        except OSError:
            return None
    return None


def _structured(state: ComputerState) -> list[StateElement]:
    return [e for e in state.elements if set(e.sources) & STRUCTURED and e.visible]


class AccessibilityGrounder:
    name = "accessibility"

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        selector = target.accessibility
        name = selector.name if selector else target.label
        role = (selector.role if selector else "") or target.role
        if not name:
            return Found(status="skipped", detail="the target has no accessible name")
        exact = selector.exact if selector else False
        pool = [e for e in _structured(state) if not role or e.role == role or (role == "textbox" and e.role in ("searchbox", "combobox"))]
        matches = [e for e in pool if _norm(e.name) == _norm(name)]
        confidence = 1.0
        if not matches and not exact:
            matches = [e for e in pool if _norm(name) in _norm(e.name) and _norm(e.name)]
            confidence = 0.85
        if not matches:
            return Found(detail=f"no {role or 'element'} named {name!r}")
        if len(matches) > 1 and selector is not None and selector.count == len(matches) and selector.index < len(matches):
            chosen = matches[selector.index]
            return Found([Candidate(self.name, confidence, chosen, reason=f"occurrence {selector.index + 1} of {len(matches)}")])
        return Found([Candidate(self.name, confidence, e, reason=f"{e.role} {e.name!r}") for e in matches])


class DOMGrounder:
    name = "dom"

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        dom = target.dom
        if dom is None or (dom.empty and not dom.tag):
            return Found(status="skipped", detail="no DOM representation recorded")
        scored: list[Candidate] = []
        for e in _structured(state):
            score, why = 0.0, []
            if dom.testid and e.attr("testid") == dom.testid:
                score += 0.6
                why.append("data-testid")
            if dom.dom_id and e.attr("dom_id") == dom.dom_id:
                score += 0.55
                why.append("id")
            if dom.resource_id and e.attr("resource_id") == dom.resource_id:
                score += 0.6
                why.append("resource-id")
            if dom.name_attr and e.attr("name") == dom.name_attr:
                score += 0.3
                why.append("name")
            if dom.href and e.attr("href") == dom.href:
                score += 0.5
                why.append("href")
            if dom.classes:
                have = set(e.attr("class").split())
                shared = len(have & set(dom.classes)) / len(dom.classes)
                if shared:
                    score += 0.25 * shared
                    why.append("class")
            if score and dom.tag and e.attr("tag") == dom.tag:
                score += 0.1
            if score and dom.type and e.attr("type") == dom.type:
                score += 0.05
            if score >= 0.3:
                scored.append(Candidate(self.name, min(1.0, score), e, reason=" + ".join(why)))
        if not scored:
            return Found(detail="no element has the recorded attributes")
        best = max(c.confidence for c in scored)
        return Found([c for c in scored if c.confidence >= best - 0.05])


class TextGrounder:
    name = "text"

    def __init__(self, threshold: float = 0.8) -> None:
        self.threshold = threshold

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        text = target.text.text if target.text else target.label
        if not text:
            return Found(status="skipped", detail="the target has no text")
        scored = [
            Candidate(self.name, round(s, 3), e, reason=f"text {e.name!r}")
            for e in _structured(state)
            if (s := _similar(text, e.name)) >= self.threshold
        ]
        if not scored:
            return Found(detail=f"no visible text like {text!r}")
        best = max(c.confidence for c in scored)
        return Found([c for c in scored if c.confidence >= best - 0.02])


class OCRGrounder:
    name = "ocr"

    def __init__(self, threshold: float = 0.75) -> None:
        self.threshold = threshold

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        text = (target.ocr.text if target.ocr else "") or target.label
        if not text:
            return Found(status="skipped", detail="the target has no text")
        lines = [e for e in state.elements if "ocr" in e.sources]
        if not lines and not state.ocr_text:
            return Found(status="unavailable", detail="no OCR in this observation")
        scored = [
            Candidate(self.name, round(s * e.confidence, 3), e, reason=f"read {e.name!r}")
            for e in lines
            if (s := _similar(text, e.name)) >= self.threshold
        ]
        if not scored:
            return Found(detail=f"{text!r} was not read on the screen")
        best = max(c.confidence for c in scored)
        return Found([c for c in scored if c.confidence >= best - 0.02])


class VisionGrounder:
    """Asks a :class:`~highhx.models.interfaces.VisionModel` where the target is. Uses the
    state's own screenshot (taken through the ``computer.state`` action), converts the box from
    screenshot pixels to input points, and binds it to the element under it when there is one.
    The model's answer is a candidate like any other: it is never acted on here."""

    name = "vision"

    def __init__(self, model: VisionModel | None) -> None:
        self.model = model

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        detected = [e for e in state.elements if "vision" in e.sources and _similar(target.label, e.name) >= 0.75]
        if detected:
            return Found([Candidate(self.name, e.confidence, e, reason=f"detected {e.name!r}") for e in detected])
        if self.model is None:
            return Found(status="unavailable", detail="no vision model is configured")
        image = _pixels(state)
        if image is None:
            return Found(status="unavailable", detail="no screenshot in this observation")
        query = (target.visual.description if target.visual else "") or target.label
        try:
            located = self.model.locate(image, query, cancel=cancel)
        except HighhXError as exc:
            return Found(status="unavailable", detail=f"vision model: {exc.message}")
        scale = state.screenshot.scale or 1.0
        out = []
        for item in located:
            x, y, w, h = item.box
            box = (round(x / scale), round(y / scale), round(w / scale), round(h / scale))
            under = state.at(center(box))
            # bind to the structured element under the box only when it is plausibly the same thing
            bound = under if under is not None and _similar(item.label, under.name) >= 0.6 else None
            out.append(Candidate(self.name, item.confidence, bound, box, f"model: {item.label!r}"))
        if not out:
            return Found(detail=f"the vision model did not find {query!r}")
        return Found(out)


class CoordinateGrounder:
    name = "coordinate"

    def __init__(self, viewport_slack: int = 8) -> None:
        self.slack = viewport_slack

    def find(self, state: ComputerState, target: Target, *, cancel: CancellationToken | None = None) -> Found:
        point = target.coordinate
        if point is None:
            return Found(status="skipped", detail="no coordinates recorded")
        if point.url and state.url and point.url.split("#")[0] != state.url.split("#")[0]:
            return Found(detail=f"recorded on {point.url}, now on {state.url}")
        if point.app and state.active_app and point.app != state.active_app:
            return Found(detail=f"recorded in {point.app}, now in {state.active_app}")
        if point.viewport and state.viewport and max(abs(a - b) for a, b in zip(point.viewport, state.viewport, strict=True)) > self.slack:
            return Found(detail=f"the viewport changed from {list(point.viewport)} to {list(state.viewport)}")
        under = state.at((point.x, point.y))
        if under is not None and target.label and _similar(target.label, under.name) < 0.5 and under.name:
            return Found(detail=f"the point now shows {under.label()}, not {target.label!r}")
        box = (point.x - 2, point.y - 2, 4, 4)
        return Found([Candidate(self.name, 0.5, under, box, "recorded point")])
