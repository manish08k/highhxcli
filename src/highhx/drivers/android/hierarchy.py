"""The uiautomator hierarchy (Android's accessibility tree) → StateElements.

Each node's class becomes a role (Button → button, EditText → textbox, …), its text or
content description its name, ``bounds="[x1,y1][x2,y2]"`` its box in device pixels, and its
resource id a stable attribute (the Android equivalent of a DOM id). Password fields never
carry their value. Containers without text that cannot be clicked are left out, so the tree
stays the size of what a person can act on.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from highhx.core.errors import IntegrationError
from highhx.perception.state import StateElement

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
ROLE_BY_CLASS = (
    ("EditText", "textbox"),
    ("AutoCompleteTextView", "textbox"),
    ("CheckBox", "checkbox"),
    ("Switch", "switch"),
    ("ToggleButton", "switch"),
    ("RadioButton", "radio"),
    ("ImageButton", "button"),
    ("Button", "button"),
    ("Spinner", "combobox"),
    ("SeekBar", "slider"),
    ("TabWidget", "tab"),
    ("ImageView", "image"),
    ("TextView", "text"),
    ("WebView", "window"),
)


def role_of(class_name: str, clickable: bool) -> str:
    short = class_name.rsplit(".", 1)[-1]
    for suffix, role in ROLE_BY_CLASS:
        if short.endswith(suffix):
            if role in ("text", "image") and clickable:
                return "button" if role == "image" else "link"
            return role
    return "button" if clickable else ""


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS.search(value or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


def parse_hierarchy(xml: str, *, limit: int = 500) -> tuple[list[StateElement], str]:
    """Elements and the visible text of a uiautomator dump."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise IntegrationError(f"The Android UI hierarchy is not valid XML: {exc}") from None
    elements: list[StateElement] = []
    texts: list[str] = []
    for node in root.iter("node"):
        a = node.attrib
        clickable = a.get("clickable") == "true" or a.get("long-clickable") == "true"
        password = a.get("password") == "true"
        class_name = a.get("class", "")
        role = role_of(class_name, clickable)
        text = (a.get("text") or "").strip()
        desc = (a.get("content-desc") or "").strip()
        hint = (a.get("hint") or "").strip()
        name = desc or text or hint
        if role == "textbox":
            name = desc or hint or a.get("resource-id", "").rsplit("/", 1)[-1] or text
        bounds = parse_bounds(a.get("bounds", ""))
        if text and not password:
            texts.append(text)
        if not role or bounds is None or (not name and not clickable and role not in ("textbox", "checkbox", "switch")):
            continue
        attributes = {
            "class": class_name,
            "resource_id": a.get("resource-id", ""),
            "package": a.get("package", ""),
            "clickable": "True" if clickable else "",
            "scrollable": "True" if a.get("scrollable") == "true" else "",
            "type": "password" if password else "",
            "selected": "True" if a.get("selected") == "true" else "",
        }
        checkable = a.get("checkable") == "true"
        elements.append(
            StateElement(
                id=f"a{len(elements) + 1}",
                role=role,
                name=name[:160],
                value="" if password else (text if role == "textbox" else ""),
                bounds=bounds,
                enabled=a.get("enabled", "true") == "true",
                focused=a.get("focused") == "true",
                checked=(a.get("checked") == "true") if checkable else None,
                visible=True,
                sources=("android",),
                attributes=tuple(sorted((k, v) for k, v in attributes.items() if v)),
            )
        )
        if len(elements) >= limit:
            break
    return elements, "\n".join(texts)[:6000]
