"""What the computer-use runtime sees and can do.

An :class:`Observation` is a semantic snapshot of a UI — controls by role and
name (``Button "Save"``, ``TextBox "Email"``), not pixels. From it the runtime
derives a finite set of :class:`ActionCandidate` s (``click:e12``,
``type:e4``, ``press:enter`` …). Deterministic automation resolves a candidate
from a selector; the AI agent may only choose among the candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs

# Roles we expose, normalised across accessibility APIs and the DOM.
ROLES = (
    "button",
    "link",
    "textbox",
    "searchbox",
    "checkbox",
    "radio",
    "combobox",
    "listbox",
    "option",
    "menuitem",
    "tab",
    "switch",
    "slider",
    "menu",
    "heading",
    "image",
    "text",
    "window",
)
TYPEABLE = frozenset({"textbox", "searchbox", "combobox"})
CLICKABLE = frozenset(
    {"button", "link", "checkbox", "radio", "menuitem", "tab", "switch", "option", "combobox", "image"}
)
KEYS = ("enter", "tab", "escape", "backspace", "arrowdown", "arrowup", "pagedown", "pageup", "space")


@dataclass
class UIElement:
    id: str
    """Stable within one observation (``e12``)."""
    role: str
    name: str
    value: str = ""
    """Current value; always empty for password and payment fields."""
    enabled: bool = True
    focused: bool = False
    checked: bool | None = None
    visible: bool = True
    attributes: dict[str, str] = field(default_factory=dict)
    """Structural facts: input type, href, form method, class, autocomplete …"""
    bounds: tuple[int, int, int, int] | None = None
    source: str = "dom"
    """dom | ax (native accessibility) | ocr | vision"""

    @property
    def secret(self) -> bool:
        kind = self.attributes.get("type", "").lower()
        auto = self.attributes.get("autocomplete", "").lower()
        return kind == "password" or "password" in auto or "cc-" in auto or "one-time-code" in auto

    def stable_label(self) -> str:
        """Role and name only — what the control *is*, without transient state (used in action
        descriptors so an approval binds to the control, not to whether it had focus)."""
        return f'{self.role} "{self.name or "(unnamed)"}"'

    def label(self) -> str:
        state = []
        if not self.enabled:
            state.append("disabled")
        if self.focused:
            state.append("focused")
        if self.checked is not None:
            state.append("checked" if self.checked else "unchecked")
        if self.secret:
            state.append("secret")
        elif self.value and self.role in TYPEABLE:
            state.append(f"value={self.value[:40]!r}")
        extra = f" ({', '.join(state)})" if state else ""
        return f'{self.role} "{self.name or "(unnamed)"}"{extra}'

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": "" if self.secret else self.value,
            "enabled": self.enabled,
            "focused": self.focused,
            "checked": self.checked,
            "attributes": self.attributes,
            "source": self.source,
        }


@dataclass
class Observation:
    provider: str
    application: str
    title: str = ""
    url: str = ""
    elements: list[UIElement] = field(default_factory=list)
    text: str = ""
    """Visible text (truncated) — untrusted content."""
    captured_at: float = 0.0

    def element(self, element_id: str) -> UIElement | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def fingerprint(self) -> tuple[str, str, str, tuple[tuple[str, str, str, bool | None], ...]]:
        """What counts as "the UI changed": location, title, visible text and control states."""
        return (
            self.url,
            self.title,
            self.text,
            tuple((e.role, e.name, "" if e.secret else e.value, e.checked) for e in self.elements),
        )

    def summary(self, limit: int = 120) -> str:
        head = f"{self.application}"
        if self.title:
            head += f" — {self.title}"
        if self.url:
            head += f" <{self.url}>"
        lines = [head, *(f"  [{e.id}] {e.label()}" for e in self.elements[:limit])]
        if len(self.elements) > limit:
            lines.append(f"  … {len(self.elements) - limit} more elements")
        return "\n".join(lines)


@dataclass(frozen=True)
class ActionCandidate:
    id: str
    """``click:e12``, ``type:e4``, ``press:enter``, ``scroll:down``, ``done``, ``ask_user``."""
    verb: str
    element: str | None = None
    description: str = ""
    needs_text: bool = False


@dataclass(frozen=True)
class Selector:
    """How deterministic automation names a control: by role and/or accessible name."""

    name: str | None = None
    role: str | None = None
    exact: bool = False
    index: int = 0

    @classmethod
    def parse(cls, text: str) -> Selector:
        """``Search`` · ``button:Search`` · ``textbox="Email"`` · ``link:Docs#2``."""
        raw = text.strip()
        index = 0
        match = re.match(r"^(.*)#(\d+)$", raw)
        if match:
            raw, index = match.group(1), int(match.group(2)) - 1
        role = None
        head, sep, tail = raw.partition(":")
        if sep and head.lower() in ROLES:
            role, raw = head.lower(), tail
        head, sep, tail = raw.partition("=")
        if sep and head.lower() in ROLES:
            role, raw = head.lower(), tail
        exact = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'"
        name = raw[1:-1] if exact else raw
        return cls(name=name or None, role=role, exact=exact, index=max(0, index))

    def matches(self, element: UIElement) -> bool:
        if self.role and element.role != self.role and not (self.role == "textbox" and element.role in TYPEABLE):
            return False
        if self.name is None:
            return True
        name = " ".join(element.name.split()).lower()
        wanted = " ".join(self.name.split()).lower()
        return name == wanted if self.exact else wanted in name


def candidates(observation: Observation) -> list[ActionCandidate]:
    """The finite set of actions that are valid in ``observation``."""
    found: list[ActionCandidate] = []
    for element in observation.elements:
        if not element.enabled or not element.visible:
            continue
        if element.role in TYPEABLE:
            found.append(ActionCandidate(f"focus:{element.id}", "focus", element.id, f"focus {element.label()}"))
            found.append(
                ActionCandidate(
                    f"type:{element.id}", "type", element.id, f"type text into {element.label()}", needs_text=True
                )
            )
        if element.role in CLICKABLE:
            found.append(ActionCandidate(f"click:{element.id}", "click", element.id, f"click {element.label()}"))
        if element.role in ("combobox", "listbox") and element.attributes.get("tag") == "select":
            found.append(
                ActionCandidate(
                    f"select:{element.id}",
                    "select",
                    element.id,
                    f"choose an option in {element.label()}",
                    needs_text=True,
                )
            )
    found += [ActionCandidate(f"press:{key}", "press", None, f"press {key}") for key in ("enter", "tab", "escape")]
    found += [
        ActionCandidate("scroll:down", "scroll", None, "scroll down"),
        ActionCandidate("scroll:up", "scroll", None, "scroll up"),
        ActionCandidate("done", "done", None, "the goal is achieved"),
        ActionCandidate("ask_user", "ask_user", None, "ask the user for help or information"),
    ]
    return found


def describe_action(
    candidate: ActionCandidate,
    observation: Observation,
    *,
    tool: str,
    actor: Actor,
    text: str | None = None,
) -> ActionDescriptor:
    """The safety description of performing ``candidate`` in ``observation``."""
    element = observation.element(candidate.element) if candidate.element else None
    focused = next((e for e in observation.elements if e.focused), None)
    application = observation.application + (f" <{observation.url}>" if observation.url else "")
    if candidate.verb == "upload" and element is not None:
        names = [p.replace("\\", "/").rsplit("/", 1)[-1] for p in (text or "").split("\n") if p]
        return ActionDescriptor(
            kind=ActionKind.UI_UPLOAD,
            summary=f"Upload {', '.join(names) or 'files'} into {element.stable_label()}",
            tool=tool,
            target=", ".join(names),
            application=application,
            actor=actor,
            attributes=attrs(
                role=element.role,
                name=element.name,
                ordinal=ordinal(observation, element),
                files_sha=_digest(text or ""),
            ),
        )
    if candidate.verb in _ELEMENT_VERBS and element is not None:
        kind = _ELEMENT_VERBS[candidate.verb]
        drop = observation.element(text) if candidate.verb == "drag" and text else None
        extra: dict[str, object] = {}
        if candidate.verb == "download":
            extra["download"] = "True"  # the classifier treats it as a download, whatever the markup says
        if drop is not None:
            extra["drop"] = drop.stable_label()
        return ActionDescriptor(
            kind=kind,
            summary=_VERB_LABELS.get(candidate.verb, candidate.verb.capitalize())
            + f" {element.stable_label()}"
            + (f" → {text!r}" if text and candidate.verb == "select" else "")
            + (f" onto {drop.stable_label()}" if drop is not None else ""),
            tool=tool,
            target=element.name or element.id,
            application=application,
            actor=actor,
            attributes=attrs(
                role=element.role,
                name=element.name,
                ordinal=ordinal(observation, element),
                option=text if candidate.verb == "select" else None,
                **{**{k: v for k, v in element.attributes.items() if k in _SAFETY_ATTRS}, **extra},
            ),
        )
    if candidate.verb == "type" and element is not None:
        shown = "••••••" if element.secret else repr((text or "")[:80])
        return ActionDescriptor(
            kind=ActionKind.UI_TYPE,
            summary=f"Type {shown} into {element.stable_label()}",
            tool=tool,
            target=element.name or element.id,
            application=application,
            actor=actor,
            attributes=attrs(
                role=element.role,
                name=element.name,
                ordinal=ordinal(observation, element),
                input_type=element.attributes.get("type", ""),
                autocomplete=element.attributes.get("autocomplete", ""),
                text_sha=_digest(text or ""),
            ),
        )
    if candidate.verb == "press":
        key = candidate.id.split(":", 1)[1]
        in_form = focused is not None and focused.attributes.get("form") not in (None, "", "False")
        return ActionDescriptor(
            kind=ActionKind.UI_KEY,
            summary=f"Press {key}" + (f" in {focused.stable_label()}" if focused else ""),
            tool=tool,
            target=focused.name if focused else "",
            application=application,
            actor=actor,
            attributes=attrs(
                key=key,
                in_form=in_form,
                form_method=focused.attributes.get("form_method", "") if focused else "",
                form_has_password=focused.attributes.get("form_has_password", "") if focused else "",
                focus_ordinal=ordinal(observation, focused) if focused else "",
                focus_name=focused.name if focused else "",
            ),
        )
    return ActionDescriptor(
        kind=ActionKind.UI_SCROLL,
        summary=candidate.description.capitalize(),
        tool=tool,
        application=application,
        actor=actor,
    )


def identity(element: UIElement) -> tuple[str, str, str, str, str]:
    """What an element *is*, independent of the id it got in one observation."""
    a = element.attributes
    return (
        element.role,
        " ".join(element.name.split()).lower(),
        a.get("tag", ""),
        a.get("type", ""),
        a.get("href", ""),
    )


def ordinal(observation: Observation, element: UIElement) -> int:
    """Position of ``element`` among elements with the same identity (distinguishes two "Delete" buttons)."""
    key = identity(element)
    same = [e for e in observation.elements if identity(e) == key]
    return next((i for i, e in enumerate(same) if e.id == element.id), 0)


def rebind(element: UIElement, observation: Observation, index: int) -> UIElement | None:
    """The element in a newer ``observation`` that is the same control as ``element``."""
    key = identity(element)
    same = [e for e in observation.elements if identity(e) == key]
    return same[index] if index < len(same) else None


_ELEMENT_VERBS = {
    "click": ActionKind.UI_CLICK,
    "focus": ActionKind.UI_SCROLL,
    "select": ActionKind.UI_SELECT,
    "hover": ActionKind.UI_SCROLL,
    "double_click": ActionKind.UI_CLICK,
    "right_click": ActionKind.UI_CLICK,
    "download": ActionKind.UI_CLICK,
    "drag": ActionKind.UI_CLICK,
}
_VERB_LABELS = {
    "double_click": "Double-click",
    "right_click": "Right-click",
    "hover": "Hover over",
    "download": "Download via",
    "drag": "Drag",
}

_SAFETY_ATTRS = frozenset(
    {"type", "href", "form_method", "form_has_password", "class", "download", "value", "title", "tag"}
)


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
