"""Selectors that survive a changing UI: one target, several independent representations.

    Target("Invoices", role="link")
      semantic        what it is:          label "Invoices", role link, "the invoices section"
      accessibility   role + accessible name, which occurrence (index of count)
      dom             id, data-testid, name attribute, tag, type, href, stable class tokens
      text            the visible text
      ocr             the text as read from pixels
      visual          a description for a vision model, the last box and screenshot digest
      coordinate      the last point, with the viewport/window and URL it was valid for

A class rename breaks only the DOM representation; a reworded label only the text ones; a moved
layout only the coordinate one. Grounding tries them in order and records which one worked, and
healing (:meth:`Target.healed`) refreshes the representations that drifted from the element that
was found. Coordinates are kept as the last resort and are only used again inside the same
viewport and page.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.perception.state import ComputerState, StateElement

_VOLATILE_CLASS = re.compile(r"^(?:css-|sc-|jsx-|_|svelte-|ng-|ember)|[0-9a-f]{6,}|\d{3,}")
"""Generated class names (CSS-in-JS hashes, framework prefixes) that change on every build."""


def stable_classes(value: str) -> tuple[str, ...]:
    return tuple(c for c in value.split() if c and not _VOLATILE_CLASS.search(c))[:6]


@dataclass(frozen=True)
class SemanticSelector:
    label: str
    role: str = ""
    description: str = ""


@dataclass(frozen=True)
class AccessibilitySelector:
    name: str
    role: str = ""
    exact: bool = True
    index: int = 0
    """Which of the elements with this role and name (tree order)."""
    count: int = 1
    """How many there were when it was recorded (the index is trusted only while it is the same)."""


@dataclass(frozen=True)
class DOMSelector:
    tag: str = ""
    dom_id: str = ""
    testid: str = ""
    name_attr: str = ""
    type: str = ""
    href: str = ""
    classes: tuple[str, ...] = ()
    resource_id: str = ""
    """Android view ids (``com.app:id/save``) — the Android tree's equivalent of a DOM id."""

    @property
    def empty(self) -> bool:
        return not any((self.dom_id, self.testid, self.name_attr, self.href, self.classes, self.resource_id))


@dataclass(frozen=True)
class TextSelector:
    text: str
    exact: bool = False


@dataclass(frozen=True)
class OCRSelector:
    text: str


@dataclass(frozen=True)
class VisualSelector:
    description: str
    box: tuple[int, int, int, int] | None = None
    screenshot_sha: str = ""


@dataclass(frozen=True)
class CoordinateSelector:
    x: int
    y: int
    viewport: tuple[int, int] | None = None
    app: str = ""
    url: str = ""


@dataclass(frozen=True)
class Target:
    label: str
    role: str = ""
    semantic: SemanticSelector | None = None
    accessibility: AccessibilitySelector | None = None
    dom: DOMSelector | None = None
    text: TextSelector | None = None
    ocr: OCRSelector | None = None
    visual: VisualSelector | None = None
    coordinate: CoordinateSelector | None = None
    history: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    """Heals applied to this target (what changed, when)."""

    @classmethod
    def of(cls, label: str, role: str = "", *, description: str = "") -> Target:
        """A target known only by what it is (nothing recorded yet)."""
        return cls(
            label,
            role,
            semantic=SemanticSelector(label, role, description),
            accessibility=AccessibilitySelector(label, role, exact=False) if label else None,
            text=TextSelector(label) if label else None,
            ocr=OCRSelector(label) if label else None,
            visual=VisualSelector(description or (f"the {role} labelled {label!r}" if role else repr(label))),
        )

    @classmethod
    def from_element(cls, element: StateElement, state: ComputerState, *, description: str = "") -> Target:
        """Every representation of ``element`` as it is in ``state`` (used when recording)."""
        same = [e for e in state.elements if e.role == element.role and _norm(e.name) == _norm(element.name)]
        index = next((i for i, e in enumerate(same) if e.id == element.id), 0)
        dom = DOMSelector(
            tag=element.attr("tag"),
            dom_id=element.attr("dom_id"),
            testid=element.attr("testid"),
            name_attr=element.attr("name"),
            type=element.attr("type"),
            href=element.attr("href"),
            classes=stable_classes(element.attr("class")),
            resource_id=element.attr("resource_id"),
        )
        point = element.center
        label = element.name
        return cls(
            label,
            element.role,
            semantic=SemanticSelector(label, element.role, description),
            accessibility=AccessibilitySelector(label, element.role, True, index, len(same)) if label else None,
            dom=dom if not dom.empty or dom.tag else None,
            text=TextSelector(label, exact=True) if label else None,
            ocr=OCRSelector(label) if label else None,
            visual=VisualSelector(
                description or f"the {element.role} labelled {label!r}",
                element.bounds,
                state.screenshot.sha256 if state.screenshot else "",
            ),
            coordinate=CoordinateSelector(point[0], point[1], state.viewport, state.active_app, state.url) if point else None,
        )

    def healed(self, element: StateElement, state: ComputerState, *, strategy: str, when: str = "") -> Target:
        """This target refreshed from the element grounding found: drifted representations are
        replaced, the semantic description is kept, and the change is remembered."""
        fresh = Target.from_element(element, state, description=self.semantic.description if self.semantic else "")
        changed = [
            name
            for name in ("accessibility", "dom", "text", "coordinate")
            if getattr(self, name) is not None and getattr(fresh, name) != getattr(self, name)
        ]
        entry = {"strategy": strategy, "changed": changed, "label": element.name, "when": when}
        semantic = self.semantic or fresh.semantic
        return replace(
            fresh,
            label=self.label if self.label and not element.name else fresh.label,
            semantic=semantic,
            visual=replace(fresh.visual, description=self.visual.description)
            if self.visual and fresh.visual
            else fresh.visual,
            history=(*self.history, entry) if changed else self.history,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label, "role": self.role}
        for name in ("semantic", "accessibility", "dom", "text", "ocr", "visual", "coordinate"):
            value = getattr(self, name)
            if value is not None:
                data = asdict(value)
                for key, item in list(data.items()):
                    if isinstance(item, tuple):
                        data[key] = list(item)
                out[name] = data
        if self.history:
            out["history"] = [dict(h) for h in self.history]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Target:
        def build(kind: Any, raw: Any) -> Any:
            if not isinstance(raw, dict):
                return None
            fields = {k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items()}
            return kind(**fields)

        return cls(
            str(data.get("label", "")),
            str(data.get("role", "")),
            semantic=build(SemanticSelector, data.get("semantic")),
            accessibility=build(AccessibilitySelector, data.get("accessibility")),
            dom=build(DOMSelector, data.get("dom")),
            text=build(TextSelector, data.get("text")),
            ocr=build(OCRSelector, data.get("ocr")),
            visual=build(VisualSelector, data.get("visual")),
            coordinate=build(CoordinateSelector, data.get("coordinate")),
            history=tuple(dict(h) for h in data.get("history") or ()),
        )

    @classmethod
    def parse(cls, text: str) -> Target:
        """From the selector syntax people type (``button:Save``, ``link:"Docs"#2``, ``Save``)."""
        from highhx.computer.model import Selector

        selector = Selector.parse(text)
        label = selector.name or ""
        target = cls.of(label, selector.role or "")
        if selector.exact or selector.index:
            target = replace(
                target,
                accessibility=AccessibilitySelector(label, selector.role or "", selector.exact, selector.index, 0),
            )
        return target


def _norm(text: str) -> str:
    return " ".join(text.lower().split())
