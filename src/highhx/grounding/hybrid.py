"""HybridGrounder: try the representations cheapest and most reliable first, score the
candidates, stop at the first confident and unambiguous answer, and record every attempt.

    accessibility (1.00) → dom (0.95) → text (0.85) → ocr (0.70) → vision (0.65) → coordinate (0.30)

score = strategy weight * candidate confidence (+ agreement bonus when an earlier strategy
pointed at the same element). A strategy whose best candidates tie is *ambiguous*. The grounder
then tries to break the tie with the other representations: DOM attributes, the recorded box,
the recorded point. If the tie remains, it moves on and finally reports ``ambiguous`` with the
candidates. It never picks one of several equals.

Weights are per task (``weights=``), so a canvas-heavy app can put OCR and vision first and a
form can rely on accessibility alone. Perception levels that the current state lacks (OCR,
vision) can be fetched on demand through ``escalate``. That runs the ``computer.state`` action
again with a higher perception policy, so the capture is still policy-checked and audited.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.grounding.grounders import (
    AccessibilityGrounder,
    Candidate,
    CoordinateGrounder,
    DOMGrounder,
    Found,
    Grounder,
    OCRGrounder,
    TextGrounder,
    VisionGrounder,
)
from highhx.perception.state import ComputerState, center, overlap

if TYPE_CHECKING:
    from highhx.execution.cancellation import CancellationToken
    from highhx.grounding.selectors import Target
    from highhx.models.interfaces import VisionModel

ORDER = ("accessibility", "dom", "text", "ocr", "vision", "coordinate")
WEIGHTS: dict[str, float] = {
    "accessibility": 1.0,
    "dom": 0.95,
    "text": 0.85,
    "ocr": 0.7,
    "vision": 0.65,
    "coordinate": 0.3,
}
AGREEMENT_BONUS = 0.15
TIE_MARGIN = 0.05
NEEDS_LEVEL = {"ocr": "ocr", "vision": "vision"}
"""Strategies that need a perception level the state may not have yet."""


@dataclass(frozen=True)
class GroundingAttempt:
    strategy: str
    result: str
    """success · failed · ambiguous · unavailable · skipped"""
    candidates: int = 0
    best: float = 0.0
    detail: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "result": self.result,
            "candidates": self.candidates,
            "best": round(self.best, 3),
            "detail": self.detail,
            "seconds": round(self.seconds, 4),
        }


@dataclass
class GroundingResult:
    target: Target
    status: str
    """grounded · ambiguous · not_found"""
    candidate: Candidate | None = None
    attempts: list[GroundingAttempt] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)
    state: ComputerState | None = None
    """The observation the answer is about (after any escalation)."""

    @property
    def grounded(self) -> bool:
        return self.status == "grounded" and self.candidate is not None

    @property
    def strategy(self) -> str:
        return self.candidate.strategy if self.candidate else ""

    @property
    def point(self) -> tuple[int, int] | None:
        return self.candidate.point if self.candidate else None

    def records(self) -> tuple[dict[str, Any], ...]:
        """The attempts as plain data (``ActionRequest.grounding``)."""
        return tuple(a.to_dict() for a in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target.to_dict(),
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "alternatives": [c.to_dict() for c in self.alternatives[:5]],
        }

    def explain(self) -> str:
        lines = [f"{self.target.label or '(target)'}: {self.status}" + (f" by {self.strategy}" if self.strategy else "")]
        for a in self.attempts:
            lines.append(f"  {a.strategy:<13} {a.result:<11} {a.detail}")
        return "\n".join(lines)


Escalate = Callable[[str, str], ComputerState | None]
"""``escalate(level, query)`` → a new state with that perception level (ocr · vision), or None."""


class HybridGrounder:
    def __init__(
        self,
        *,
        vision: VisionModel | None = None,
        grounders: Sequence[Grounder] | None = None,
        order: Sequence[str] = ORDER,
        weights: Mapping[str, float] | None = None,
        min_score: float = 0.5,
        emit: Callable[..., Any] | None = None,
    ) -> None:
        available = {
            g.name: g
            for g in (
                grounders
                or (
                    AccessibilityGrounder(),
                    DOMGrounder(),
                    TextGrounder(),
                    OCRGrounder(),
                    VisionGrounder(vision),
                    CoordinateGrounder(),
                )
            )
        }
        self.grounders = available
        self.order = [name for name in order if name in available]
        self.weights = {**WEIGHTS, **(weights or {})}
        self.min_score = min_score
        self.emit = emit

    def ground(
        self,
        state: ComputerState,
        target: Target,
        *,
        strategies: Sequence[str] | None = None,
        escalate: Escalate | None = None,
        cancel: CancellationToken | None = None,
    ) -> GroundingResult:
        order = [s for s in (strategies or self.order) if s in self.grounders]
        result = GroundingResult(target, "not_found", state=state)
        seen: dict[str, float] = {}
        """candidate key → best earlier score (for agreement)"""
        ambiguous: list[Candidate] = []
        self._emit("grounding.started", target=target.label, role=target.role, strategies=order)
        for name in order:
            started = time.monotonic()
            current = result.state or state
            found = self.grounders[name].find(current, target, cancel=cancel)
            if found.status == "unavailable" and name in NEEDS_LEVEL and escalate is not None:
                richer = escalate(NEEDS_LEVEL[name], target.label)
                if richer is not None:
                    result.state = current = richer
                    found = self.grounders[name].find(current, target, cancel=cancel)
            attempt, chosen, tied = self._judge(name, found, target, seen, time.monotonic() - started)
            result.attempts.append(attempt)
            self._emit("grounding.attempt", **attempt.to_dict())
            for c in found.candidates:
                seen[c.key] = max(seen.get(c.key, 0.0), c.score or self.weights.get(name, 0.5) * c.confidence)
            if chosen is not None:
                result.status, result.candidate = "grounded", chosen
                result.alternatives = [c for c in found.candidates if c is not chosen]
                break
            if tied:
                ambiguous = tied
        if result.candidate is None and ambiguous:
            result.status, result.alternatives = "ambiguous", ambiguous
        self._emit(
            "grounding.completed",
            target=target.label,
            status=result.status,
            strategy=result.strategy,
            point=list(result.point) if result.point else None,
            attempts=[a.to_dict() for a in result.attempts],
        )
        return result

    # ---------------------------------------------------------------- judging
    def _judge(
        self, name: str, found: Found, target: Target, seen: dict[str, float], seconds: float
    ) -> tuple[GroundingAttempt, Candidate | None, list[Candidate]]:
        if found.status in ("unavailable", "skipped"):
            return GroundingAttempt(name, found.status, 0, 0.0, found.detail, seconds), None, []
        if not found.candidates:
            return GroundingAttempt(name, "failed", 0, 0.0, found.detail, seconds), None, []
        weight = self.weights.get(name, 0.5)
        scored = sorted(
            (
                Candidate(
                    c.strategy,
                    c.confidence,
                    c.element,
                    c.box,
                    c.reason,
                    min(1.0, weight * c.confidence + (AGREEMENT_BONUS if c.key in seen else 0.0)),
                )
                for c in found.candidates
            ),
            key=lambda c: c.score,
            reverse=True,
        )
        best = scored[0]
        tied = [c for c in scored if best.score - c.score <= TIE_MARGIN]
        if len(tied) > 1:
            broken = self._break_tie(tied, target)
            if broken is not None:
                best, tied = broken, [broken]
        if best.score < self.min_score:
            return GroundingAttempt(name, "failed", len(scored), best.score, f"best score {best.score:.2f} is too low", seconds), None, []
        if len(tied) > 1:
            names = ", ".join(c.reason for c in tied[:4])
            return GroundingAttempt(name, "ambiguous", len(scored), best.score, f"{len(tied)} equally good: {names}", seconds), None, tied
        return GroundingAttempt(name, "success", len(scored), best.score, best.reason, seconds), best, []

    @staticmethod
    def _break_tie(tied: list[Candidate], target: Target) -> Candidate | None:
        """One of equals, by the representations the tied strategy did not use: DOM attributes,
        the recorded box, the recorded point. None when they do not decide."""
        dom = target.dom
        if dom is not None and not dom.empty:
            def dom_score(c: Candidate) -> int:
                e = c.element
                if e is None:
                    return 0
                return sum(
                    (
                        bool(dom.testid) and e.attr("testid") == dom.testid,
                        bool(dom.dom_id) and e.attr("dom_id") == dom.dom_id,
                        bool(dom.resource_id) and e.attr("resource_id") == dom.resource_id,
                        bool(dom.href) and e.attr("href") == dom.href,
                        bool(dom.name_attr) and e.attr("name") == dom.name_attr,
                    )
                )

            ranked = sorted(tied, key=dom_score, reverse=True)
            if dom_score(ranked[0]) > dom_score(ranked[1]):
                return ranked[0]
        if target.visual is not None and target.visual.box is not None:
            box = target.visual.box
            ranked = sorted(tied, key=lambda c: overlap(c.element.bounds, box) if c.element and c.element.bounds else 0.0, reverse=True)
            first = overlap(ranked[0].element.bounds, box) if ranked[0].element and ranked[0].element.bounds else 0.0
            second = overlap(ranked[1].element.bounds, box) if ranked[1].element and ranked[1].element.bounds else 0.0
            if first >= 0.5 and first - second >= 0.3:
                return ranked[0]
        if target.coordinate is not None:
            point = (target.coordinate.x, target.coordinate.y)

            def distance(c: Candidate) -> float:
                p = c.point
                return float("inf") if p is None else ((p[0] - point[0]) ** 2 + (p[1] - point[1]) ** 2) ** 0.5

            ranked = sorted(tied, key=distance)
            if distance(ranked[0]) < 24 and distance(ranked[1]) > 3 * max(distance(ranked[0]), 8):
                return ranked[0]
        return None

    def _emit(self, name: str, **data: Any) -> None:
        if self.emit is not None:
            self.emit(name, **data)


def point_of(result: GroundingResult) -> tuple[int, int] | None:
    if result.candidate is None:
        return None
    if result.candidate.element is not None and result.candidate.element.bounds:
        return center(result.candidate.element.bounds)
    return result.candidate.point
