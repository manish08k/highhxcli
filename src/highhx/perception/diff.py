"""What changed between two observations: pixels (VisualDiff) and structure (StateDiff).

VisualDiff compares two screenshots cell by cell (a grid of mean luminance), which tolerates
anti-aliasing and compression noise. It reports how much of the screen changed and where, as
boxes in screenshot pixels. Very large images, or formats the built-in decoder does not read,
fall back to comparing digests (``method: digest``): the result can then only be "same" or
"changed everywhere", and it says so.

StateDiff compares two ComputerStates structurally: URL, title, focus, and which elements
appeared, disappeared, moved or changed. It is the cheap check, and the agent loop uses it before
any pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.perception.png import PNGError, decode
from highhx.perception.tracker import ElementTracker

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState, ScreenshotRef

MAX_PIXELS = 4_000_000
"""Larger images are compared by digest (pure-Python decoding is linear but not fast)."""


@dataclass(frozen=True)
class VisualDiffResult:
    method: str
    """pixels · digest"""
    changed: bool
    ratio: float
    """Fraction of grid cells (or 1.0/0.0 by digest) that changed."""
    regions: tuple[tuple[int, int, int, int], ...] = ()
    """Changed areas in screenshot pixels (x, y, w, h), merged per row band."""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "changed": self.changed,
            "ratio": round(self.ratio, 4),
            "regions": [list(r) for r in self.regions],
            "detail": self.detail,
        }


class VisualDiff:
    def __init__(self, *, cell: int = 16, threshold: int = 12, min_ratio: float = 0.002) -> None:
        self.cell = cell
        """Grid cell size in pixels."""
        self.threshold = threshold
        """Mean-luminance difference (0-255) for a cell to count as changed."""
        self.min_ratio = min_ratio
        """Below this fraction of changed cells, the screens count as the same (noise)."""

    def compare(self, before: ScreenshotRef | bytes, after: ScreenshotRef | bytes) -> VisualDiffResult:
        a, b = _bytes(before), _bytes(after)
        if a is None or b is None:
            same = _digest(before) == _digest(after) and bool(_digest(before))
            return VisualDiffResult("digest", not same, 0.0 if same else 1.0, detail="pixels not available")
        if a == b:
            return VisualDiffResult("pixels", False, 0.0)
        try:
            from highhx.perception.png import png_size

            wa, ha = png_size(a)
            wb, hb = png_size(b)
            if (wa, ha) != (wb, hb):
                return VisualDiffResult("pixels", True, 1.0, ((0, 0, wb, hb),), "the screen size changed")
            if wa * ha > MAX_PIXELS:
                return VisualDiffResult("digest", True, 1.0, ((0, 0, wb, hb),), "image too large for pixel diff")
            ia, ib = decode(a), decode(b)
        except PNGError as exc:
            return VisualDiffResult("digest", True, 1.0, detail=str(exc))
        grid_a, grid_b = self._grid(ia.luminance(), ia.width, ia.height), self._grid(ib.luminance(), ib.width, ib.height)
        cols = (ia.width + self.cell - 1) // self.cell
        changed_cells = [i for i, (x, y) in enumerate(zip(grid_a, grid_b, strict=True)) if abs(x - y) >= self.threshold]
        ratio = len(changed_cells) / max(1, len(grid_a))
        regions = self._regions(changed_cells, cols, ia.width, ia.height)
        return VisualDiffResult("pixels", ratio >= self.min_ratio, ratio, regions)

    def _grid(self, lum: list[int], width: int, height: int) -> list[float]:
        cell = self.cell
        cols = (width + cell - 1) // cell
        rows = (height + cell - 1) // cell
        sums = [0] * (cols * rows)
        counts = [0] * (cols * rows)
        for y in range(height):
            base = y * width
            row = (y // cell) * cols
            for x in range(width):
                i = row + x // cell
                sums[i] += lum[base + x]
                counts[i] += 1
        return [s / c if c else 0.0 for s, c in zip(sums, counts, strict=True)]

    def _regions(self, cells: list[int], cols: int, width: int, height: int) -> tuple[tuple[int, int, int, int], ...]:
        """Changed cells merged into boxes: contiguous runs per grid row, then rows with
        overlapping runs stacked."""
        cell = self.cell
        runs: list[list[int]] = []  # [row, c0, c1]
        for i in sorted(cells):
            row, col = divmod(i, cols)
            if runs and runs[-1][0] == row and runs[-1][2] == col - 1:
                runs[-1][2] = col
            else:
                runs.append([row, col, col])
        boxes: list[list[int]] = []  # [r0, r1, c0, c1]
        for row, c0, c1 in runs:
            for box in boxes:
                if box[1] == row - 1 and c0 <= box[3] and c1 >= box[2]:
                    box[1], box[2], box[3] = row, min(box[2], c0), max(box[3], c1)
                    break
            else:
                boxes.append([row, row, c0, c1])
        out = []
        for r0, r1, c0, c1 in boxes:
            x, y = c0 * cell, r0 * cell
            out.append((x, y, min(width, (c1 + 1) * cell) - x, min(height, (r1 + 1) * cell) - y))
        return tuple(out)


def _bytes(value: ScreenshotRef | bytes) -> bytes | None:
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if value.data is not None:
        return value.data
    if value.path:
        try:
            from pathlib import Path

            return Path(value.path).read_bytes()
        except OSError:
            return None
    return None


def _digest(value: ScreenshotRef | bytes) -> str:
    if isinstance(value, bytes | bytearray):
        import hashlib

        return hashlib.sha256(value).hexdigest()
    return value.sha256


@dataclass(frozen=True)
class StateDiff:
    changed: bool
    url_changed: bool = False
    title_changed: bool = False
    focus_changed: bool = False
    app_changed: bool = False
    appeared: tuple[str, ...] = ()
    disappeared: tuple[str, ...] = ()
    moved: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    text_added: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "url_changed": self.url_changed,
            "title_changed": self.title_changed,
            "focus_changed": self.focus_changed,
            "app_changed": self.app_changed,
            "appeared": list(self.appeared),
            "disappeared": list(self.disappeared),
            "moved": list(self.moved),
            "modified": list(self.modified),
            "text_added": self.text_added[:500],
        }

    @classmethod
    def between(cls, before: ComputerState, after: ComputerState) -> StateDiff:
        tracker = ElementTracker()
        tracker.update(before)
        changes = tracker.update(after)
        old_lines = set(before.all_text.splitlines())
        added = [line for line in after.all_text.splitlines() if line.strip() and line not in old_lines]
        focus_before = before.element(before.focused)
        focus_after = after.element(after.focused)
        diff = cls(
            changed=before.fingerprint() != after.fingerprint(),
            url_changed=before.url != after.url,
            title_changed=before.title != after.title,
            focus_changed=(focus_before.label() if focus_before else "") != (focus_after.label() if focus_after else ""),
            app_changed=before.active_app != after.active_app,
            appeared=tuple(e.label() for e in changes.appeared),
            disappeared=tuple(e.label() for e in changes.disappeared),
            moved=tuple(e.label() for e in changes.moved),
            modified=tuple(e.label() for e in changes.modified),
            text_added="\n".join(added),
        )
        return diff
