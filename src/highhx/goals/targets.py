"""Naming an element generically, discovered from the page as it is now.

    e12                        the element with that id in the latest observation
    Search                     any element whose accessible name contains "Search"
    button:Search              a button named … (roles: button, link, textbox, …)
    textbox="Email"            exact name
    link#2                     the second link
    link[href*=/watch]#1       the first link whose href contains "/watch"
    [placeholder^=Search]      any element whose placeholder starts with "Search"

Nothing here knows a website: the role, name and attributes come from the accessibility tree /
DOM of whatever page is open (:class:`highhx.computer.model.Observation`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from highhx.computer.model import ROLES, Observation, Selector, UIElement

_ID = re.compile(r"^e\d+$")
_ATTR = re.compile(r"\[(?P<name>[\w:-]+)\s*(?P<op>\*=|\^=|\$=|=)\s*(?P<value>[^\]]*)\]")
_INDEX = re.compile(r"#(\d+)$")


@dataclass(frozen=True)
class AttributeFilter:
    name: str
    op: str
    value: str

    def matches(self, element: UIElement) -> bool:
        actual = str(element.attributes.get(self.name) or "")
        if self.name == "value":
            actual = element.value
        wanted = self.value.strip().strip("\"'")
        if self.op == "=":
            return actual == wanted
        if self.op == "^=":
            return actual.startswith(wanted)
        if self.op == "$=":
            return actual.endswith(wanted)
        return wanted in actual


@dataclass(frozen=True)
class Target:
    raw: str
    element_id: str | None = None
    selector: Selector | None = None
    filters: tuple[AttributeFilter, ...] = ()

    def find_all(self, observation: Observation) -> list[UIElement]:
        usable = [e for e in observation.elements if e.visible and e.enabled]
        if self.element_id is not None:
            return [e for e in usable if e.id == self.element_id]
        assert self.selector is not None
        found = [e for e in usable if self.selector.matches(e) and all(f.matches(e) for f in self.filters)]
        if not found and self.selector.name and not self.selector.exact:
            exact = Selector(self.selector.name, self.selector.role, exact=True)
            found = [e for e in usable if exact.matches(e) and all(f.matches(e) for f in self.filters)]
        return found

    def find(self, observation: Observation) -> UIElement | None:
        found = self.find_all(observation)
        index = self.selector.index if self.selector is not None else 0
        return found[index] if len(found) > index else None


def parse_target(text: str) -> Target:
    """Parse a target; raises ValueError when it cannot name anything."""
    raw = text.strip()
    if not raw:
        raise ValueError("empty target")
    if len(raw) > 300:
        raise ValueError("target is too long")
    if _ID.match(raw):
        return Target(raw, element_id=raw)
    filters = tuple(AttributeFilter(m.group("name"), m.group("op"), m.group("value")) for m in _ATTR.finditer(raw))
    rest = _ATTR.sub("", raw)
    index_suffix = ""
    match = _INDEX.search(rest)
    if match:
        index_suffix, rest = match.group(0), rest[: match.start()]
    if "[" in rest or "]" in rest:
        raise ValueError(f"cannot read the attribute filter in {text!r} (use [name*=value])")
    rest = rest.strip()
    if rest.lower() in ROLES:  # "link#2", "button[type=submit]": a role alone
        rest = f"{rest.lower()}:"
    selector = Selector.parse(rest + index_suffix) if rest or index_suffix else Selector()
    if selector.name is None and selector.role is None and not filters:
        raise ValueError(f"{text!r} names no role, name or attribute")
    return Target(raw, selector=selector, filters=filters)
