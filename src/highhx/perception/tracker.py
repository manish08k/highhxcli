"""ElementTracker: the same control across observations, whatever id each observation gave it.

Element ids (``e12``, ``o3``) are only stable within one observation. The tracker gives each
control a track id that follows it across observations: by identity first (role, name, tag, type,
href, test id), the ordinal among identical controls second, and the box last (for unnamed
elements). It reports what appeared, disappeared, moved or changed state. Verifiers use it for
conditions like "the Submit button disappears", and grounding uses it to prefer the control
that was there before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from highhx.perception.state import overlap

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState, StateElement

MOVE_SLACK = 4


def identity(element: StateElement) -> tuple[str, ...]:
    return (
        element.role,
        " ".join(element.name.lower().split()),
        element.attr("tag"),
        element.attr("type"),
        element.attr("href"),
        element.attr("testid") or element.attr("dom_id") or element.attr("resource_id"),
    )


@dataclass
class TrackChanges:
    appeared: list[StateElement] = field(default_factory=list)
    disappeared: list[StateElement] = field(default_factory=list)
    moved: list[StateElement] = field(default_factory=list)
    modified: list[StateElement] = field(default_factory=list)
    kept: list[StateElement] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.appeared or self.disappeared or self.moved or self.modified)


class ElementTracker:
    def __init__(self) -> None:
        self._tracks: dict[str, StateElement] = {}
        self._by_element: dict[str, str] = {}
        """element id in the latest state → track id"""
        self._next = 0

    def track_of(self, element_id: str) -> str | None:
        return self._by_element.get(element_id)

    def element_for(self, track_id: str) -> StateElement | None:
        return self._tracks.get(track_id)

    def update(self, state: ComputerState) -> TrackChanges:
        changes = TrackChanges()
        first = not self._tracks
        unmatched = dict(self._tracks)
        assigned: dict[str, str] = {}
        groups: dict[tuple[str, ...], list[StateElement]] = {}
        for element in state.elements:
            groups.setdefault(identity(element), []).append(element)
        old_groups: dict[tuple[str, ...], list[str]] = {}
        for tid, old in self._tracks.items():
            old_groups.setdefault(identity(old), []).append(tid)
        for key, elements in groups.items():
            candidates = [t for t in old_groups.get(key, []) if t in unmatched]
            for index, element in enumerate(elements):
                track_id: str | None = None
                if key[1] or key[5]:  # a named (or test-id'd) control: identity + ordinal
                    track_id = candidates[index] if index < len(candidates) else None
                elif element.bounds:  # unnamed: the closest box of the same identity
                    best = max(
                        (t for t in candidates if t in unmatched and self._tracks[t].bounds),
                        key=lambda t: overlap(self._tracks[t].bounds, element.bounds),  # type: ignore[arg-type]
                        default=None,
                    )
                    if best is not None and overlap(self._tracks[best].bounds, element.bounds) > 0.3:  # type: ignore[arg-type]
                        track_id = best
                if track_id is None or track_id not in unmatched:
                    self._next += 1
                    track_id = f"t{self._next}"
                    if not first:  # the very first observation has nothing to "appear" against
                        changes.appeared.append(element)
                else:
                    previous = unmatched.pop(track_id)
                    if previous.bounds and element.bounds and _moved(previous.bounds, element.bounds):
                        changes.moved.append(element)
                    elif (previous.value, previous.checked, previous.enabled, previous.focused) != (
                        element.value,
                        element.checked,
                        element.enabled,
                        element.focused,
                    ):
                        changes.modified.append(element)
                    else:
                        changes.kept.append(element)
                assigned[track_id] = element.id
                self._tracks[track_id] = element
        for track_id, element in unmatched.items():
            changes.disappeared.append(element)
            del self._tracks[track_id]
        self._by_element = {element_id: track_id for track_id, element_id in assigned.items()}
        return changes


def _moved(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return max(abs(x - y) for x, y in zip(a, b, strict=True)) > MOVE_SLACK
