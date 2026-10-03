"""StateFusion: the sources' partial views → one immutable ComputerState.

    structured (DOM / AX / Android)  kept as they are. Duplicates across trees (same role and
                                     name, overlapping boxes) become one element with both sources
    OCR lines                        pixels → points (÷ screenshot scale). A line inside a structured
                                     element that names the same text corroborates it (source
                                     added). Any other line becomes a ``text`` element (source ocr)
    vision elements                  the same, with the model's confidence

An OCR or vision element never replaces a structured one: structure has exact names, states and
ids that the executor can bind an action to. Pixels only add what the trees do not have.
"""

from __future__ import annotations

from dataclasses import replace

from highhx.perception.providers import Perceived
from highhx.perception.state import Bounds, ComputerState, StateElement, overlap

DUPLICATE_IOU = 0.6
CORROBORATE_IOU = 0.2


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def to_points(bounds: Bounds, scale: float) -> Bounds:
    if not scale or scale == 1.0:
        return bounds
    x, y, w, h = bounds
    return round(x / scale), round(y / scale), round(w / scale), round(h / scale)


def _inside(inner: Bounds, outer: Bounds, slack: int = 4) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox - slack and iy >= oy - slack and ix + iw <= ox + ow + slack and iy + ih <= oy + oh + slack


class StateFusion:
    def fuse(self, perceived: Perceived) -> ComputerState:
        elements = self._dedupe(perceived.structured)
        scale = perceived.screenshot.scale if perceived.screenshot else 1.0
        for prefix, extra in (("o", perceived.ocr), ("v", perceived.vision)):
            elements = self._merge_pixels(elements, extra, scale, prefix)
        focused = next((e.id for e in elements if e.focused), "")
        return ComputerState(
            surface=perceived.surface,
            device=perceived.device,
            active_app=perceived.active_app,
            active_window=perceived.active_window,
            windows=tuple(perceived.windows),
            processes=tuple(perceived.processes),
            cursor=perceived.cursor,
            focused=focused,
            viewport=perceived.viewport,
            browser=perceived.browser,
            elements=tuple(elements),
            text=perceived.text,
            ocr_text="\n".join(e.name for e in perceived.ocr if e.name),
            screenshot=perceived.screenshot,
            perception=tuple(perceived.records),
            metadata=tuple(sorted(perceived.metadata.items())),
        )

    @staticmethod
    def _dedupe(elements: list[StateElement]) -> list[StateElement]:
        out: list[StateElement] = []
        for element in elements:
            twin = next(
                (
                    i
                    for i, kept in enumerate(out)
                    if kept.role == element.role
                    and _norm(kept.name) == _norm(element.name)
                    and kept.bounds
                    and element.bounds
                    and set(kept.sources) != set(element.sources)
                    and overlap(kept.bounds, element.bounds) >= DUPLICATE_IOU
                ),
                None,
            )
            if twin is None:
                out.append(element)
            else:
                kept = out[twin]
                out[twin] = replace(kept, sources=tuple(dict.fromkeys((*kept.sources, *element.sources))))
        return out

    @staticmethod
    def _merge_pixels(
        elements: list[StateElement], extra: list[StateElement], scale: float, prefix: str
    ) -> list[StateElement]:
        out = list(elements)
        n = 0
        for item in extra:
            if item.bounds is None or not item.name.strip():
                continue
            bounds = to_points(item.bounds, scale)
            wanted = _norm(item.name)
            source = item.sources[0] if item.sources else ("ocr" if prefix == "o" else "vision")
            host = next(
                (
                    i
                    for i, e in enumerate(out)
                    if e.bounds
                    and (wanted in _norm(e.name) or _norm(e.name) in wanted)
                    and _norm(e.name)
                    and (_inside(bounds, e.bounds) or overlap(bounds, e.bounds) >= CORROBORATE_IOU)
                ),
                None,
            )
            if host is not None:
                e = out[host]
                if source not in e.sources:
                    out[host] = replace(e, sources=(*e.sources, source))
                continue
            n += 1
            out.append(
                replace(
                    item,
                    id=f"{prefix}{n}",
                    role=item.role or "text",
                    bounds=bounds,
                    sources=(source,),
                )
            )
        return out
