"""The perception engine: run the sources a request needs, cheapest first, and fuse the result.

    structure (DOM / AX / Android)    always, when available (cheap, exact)
    screenshot                        only when OCR or vision will run, or when asked for
    OCR                               policy ``auto``: when the structure is empty, or when the
                                      query is not in the structured text
    vision                            policy ``auto``: when the query is still not found after
                                      OCR. ``never`` by default: a model sees the screen only
                                      when the caller allows it

A state is cached for ``cache_ttl`` seconds and dropped at once by :meth:`invalidate`, which the
agent loop calls after every action that may have changed the screen. Every source's outcome is
recorded in ``state.perception`` and emitted as ``observation.created``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from highhx.core.errors import HighhXError
from highhx.execution.cancellation import CancellationToken
from highhx.perception.fusion import StateFusion
from highhx.perception.providers import (
    OCRProvider,
    Perceived,
    ScreenshotProvider,
    StructureProvider,
    VisionProvider,
    record,
)
from highhx.perception.state import ComputerState

LEVELS = ("never", "auto", "always")


@dataclass(frozen=True)
class PerceptionPolicy:
    screenshot: bool = False
    """Capture even when no level needs pixels (e.g. for a trajectory or a visual verifier)."""
    ocr: str = "auto"
    vision: str = "never"
    cache_ttl: float = 0.5

    def __post_init__(self) -> None:
        for name in ("ocr", "vision"):
            if getattr(self, name) not in LEVELS:
                raise ValueError(f"perception {name} must be one of: {', '.join(LEVELS)}")


def _found(query: str | None, texts: Sequence[str]) -> bool:
    if not query:
        return True
    wanted = " ".join(query.lower().split())
    return any(wanted in " ".join(t.lower().split()) for t in texts if t)


class PerceptionEngine:
    def __init__(
        self,
        surface: str,
        *,
        structure: Sequence[StructureProvider] = (),
        screenshot: ScreenshotProvider | None = None,
        ocr: OCRProvider | None = None,
        vision: VisionProvider | None = None,
        fusion: StateFusion | None = None,
        emit: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.surface = surface
        self.structure = list(structure)
        self.screenshots = screenshot
        self.ocr = ocr
        self.vision = vision
        self.fusion = fusion or StateFusion()
        self.emit = emit
        self.clock = clock
        self._cache: tuple[float, tuple[Any, ...], ComputerState] | None = None
        self.observations = 0
        self.screenshots_taken = 0

    def invalidate(self) -> None:
        self._cache = None

    def observe(
        self,
        *,
        policy: PerceptionPolicy | None = None,
        query: str | None = None,
        cancel: CancellationToken | None = None,
    ) -> ComputerState:
        policy = policy or PerceptionPolicy()
        key = (policy, query)
        now = self.clock()
        if self._cache is not None and self._cache[1] == key and now - self._cache[0] <= policy.cache_ttl:
            return self._cache[2]
        perceived = Perceived(self.surface)
        for provider in self.structure:
            self._structure(provider, perceived, cancel)
        texts = [perceived.text, *(e.name for e in perceived.structured)]
        want_ocr = self.ocr is not None and (
            policy.ocr == "always" or (policy.ocr == "auto" and (not perceived.structured or not _found(query, texts)))
        )
        want_vision = self.vision is not None and policy.vision == "always"
        if want_ocr or want_vision or policy.screenshot or (self.vision is not None and policy.vision == "auto" and query):
            self._capture(perceived, cancel)
        if want_ocr and perceived.screenshot is not None and self.ocr is not None:
            self._ocr(self.ocr, perceived, cancel)
            texts += [e.name for e in perceived.ocr]
        if (
            not want_vision
            and self.vision is not None
            and policy.vision == "auto"
            and query
            and not _found(query, texts)
        ):
            want_vision = True
        if want_vision and perceived.screenshot is not None and self.vision is not None:
            self._vision(self.vision, perceived, query, cancel)
        state = self.fusion.fuse(perceived)
        self.observations += 1
        self._cache = (now, key, state)
        if self.emit is not None:
            self.emit(
                "observation.created",
                surface=state.surface,
                fingerprint=state.fingerprint(),
                elements=len(state.elements),
                url=state.url,
                app=state.active_app,
                sources=[p.to_dict() for p in state.perception],
                screenshot=bool(state.screenshot),
            )
        return state

    # ---------------------------------------------------------------- levels
    def _structure(self, provider: StructureProvider, into: Perceived, cancel: CancellationToken | None) -> None:
        started = time.monotonic()
        capability = provider.capability()
        if not capability.available:
            into.records.append(record(provider.source, "unavailable", capability.detail))
            return
        before = len(into.structured)
        try:
            provider.perceive(into, cancel=cancel)
        except HighhXError as exc:
            into.records.append(record(provider.source, "failed", exc.message, started))
            return
        into.records.append(record(provider.source, "ok", provider.name, started, len(into.structured) - before))

    def _capture(self, into: Perceived, cancel: CancellationToken | None) -> None:
        if self.screenshots is None:
            into.records.append(record("screenshot", "unavailable", "no screenshot source"))
            return
        started = time.monotonic()
        capability = self.screenshots.capability()
        if not capability.available:
            into.records.append(record("screenshot", "unavailable", capability.detail))
            return
        try:
            into.screenshot = self.screenshots.capture(cancel=cancel)
        except (HighhXError, OSError) as exc:
            into.records.append(record("screenshot", "failed", getattr(exc, "message", str(exc)), started))
            return
        self.screenshots_taken += 1
        into.records.append(record("screenshot", "ok", self.screenshots.name, started, 1))

    def _ocr(self, provider: OCRProvider, into: Perceived, cancel: CancellationToken | None) -> None:
        started = time.monotonic()
        capability = provider.capability()
        if not capability.available:
            into.records.append(record("ocr", "unavailable", capability.detail))
            return
        assert into.screenshot is not None
        try:
            into.ocr = provider.read(into.screenshot, cancel=cancel)
        except (HighhXError, OSError, ValueError) as exc:
            into.records.append(record("ocr", "failed", getattr(exc, "message", str(exc)), started))
            return
        into.records.append(record("ocr", "ok", provider.name, started, len(into.ocr)))

    def _vision(
        self, provider: VisionProvider, into: Perceived, query: str | None, cancel: CancellationToken | None
    ) -> None:
        started = time.monotonic()
        capability = provider.capability()
        if not capability.available:
            into.records.append(record("vision", "unavailable", capability.detail))
            return
        assert into.screenshot is not None
        try:
            into.vision = provider.detect(into.screenshot, query=query, cancel=cancel)
        except (HighhXError, OSError, ValueError) as exc:
            into.records.append(record("vision", "failed", getattr(exc, "message", str(exc)), started))
            return
        into.records.append(record("vision", "ok", provider.name, started, len(into.vision)))
