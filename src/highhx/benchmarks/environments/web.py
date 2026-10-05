"""A deterministic web app behind the browser-provider interface: pages, controls with roles,
names, attributes and bounds, forms that submit and buttons that change the page. The HighhX
browser runtime, flows, selectors, executor and safety gate run for real on top of it."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from highhx.computer.browser import ElementNotFoundError
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.perception.png import encode, fill, solid


def png(width: int = 80, height: int = 60, boxes: tuple[tuple[int, int, int, int], ...] = ()) -> bytes:
    rgb = solid(width, height)
    for box in boxes:
        fill(rgb, width, box, (0, 0, 0))
    return encode(width, height, bytes(rgb))

BASE = "https://shop.test"


def page_elements(path: str, state: dict[str, Any], variant: str) -> tuple[str, list[dict[str, Any]], str]:
    """(title, elements, text) for a page. ``variant`` re-skins the site (renamed labels, new
    classes, moved layout) to exercise self-healing."""
    renamed = variant == "redesign"
    if path == "/":
        elements = [
            {"role": "heading", "name": "Shop", "tag": "h1"},
            {"role": "link", "name": "Billing documents" if renamed else "Invoices", "tag": "a", "href": "/invoices", "class": "nav css-1q2w3e" if renamed else "nav nav-invoices"},
            {"role": "searchbox", "name": "Search", "tag": "input", "type": "search", "name_attr": "q", "value": state.get("typed_query", "")},
            {"role": "button", "name": "Go", "tag": "button", "type": "submit"},
            {"role": "link", "name": "Account form", "tag": "a", "href": "/form"},
            {"role": "button", "name": "Delete account", "tag": "button", "class": "btn btn-danger"},
        ]
        if renamed:
            elements.insert(0, {"role": "link", "name": "Promotions", "tag": "a", "href": "/promo"})
        text = "Shop" + (f" Results for {state['query']}" if state.get("query") else "")
        if state.get("query"):
            elements.append({"role": "text", "name": f"Results for {state['query']}", "tag": "p"})
        return "Shop", elements, text
    if path == "/invoices":
        elements = [
            {"role": "heading", "name": "Invoices", "tag": "h1"},
            {"role": "button", "name": "Download CSV" if renamed else "Export", "tag": "button", "testid": "export-invoices"},
        ]
        if state.get("exported"):
            elements.append({"role": "text", "name": "Export ready", "tag": "p"})
        return "Invoices", elements, "Invoices" + (" Export ready" if state.get("exported") else "")
    if path == "/form":
        if state.get("submitted"):
            return "Thanks", [{"role": "heading", "name": "Thank you", "tag": "h1"}], "Thank you"
        return "Account", [
            {"role": "textbox", "name": "Email", "tag": "input", "type": "email", "value": state.get("email", "")},
            {"role": "textbox", "name": "Password", "tag": "input", "type": "password", "value": ""},
            {"role": "button", "name": "Submit", "tag": "button", "type": "button"},
        ], "Account"
    return "Not found", [{"role": "heading", "name": "Not found", "tag": "h1"}], "Not found"


@dataclass
class FakeWebApp:
    variant: str = ""
    url: str = f"{BASE}/"
    state: dict[str, Any] = field(default_factory=dict)
    log: list[tuple[str, Any]] = field(default_factory=list)
    hidden_until_scroll: bool = False
    """The Export button is below the fold until the page is scrolled."""
    scrolled: bool = False
    name: str = "chrome"
    last_wait_settled: bool = True
    _ids: dict[str, dict[str, Any]] = field(default_factory=dict)

    def capability(self) -> Capability:
        return Capability("browser", True, "fake web app")

    # --------------------------------------------------------------- observe
    def observe(self, *, cancel: Any = None) -> Observation:
        path = urlparse(self.url).path or "/"
        title, elements, text = page_elements(path, self.state, self.variant)
        if self.hidden_until_scroll and not self.scrolled and path == "/invoices":
            elements = [e for e in elements if e.get("testid") != "export-invoices"]
        self._ids = {}
        out = []
        y = 20 if self.variant != "redesign" else 60
        for n, spec in enumerate(elements, 1):
            element_id = f"e{n}"
            self._ids[element_id] = spec
            attributes = {k: str(spec.get(k, "")) for k in ("tag", "type", "href", "class") if spec.get(k)}
            if spec.get("testid"):
                attributes["testid"] = spec["testid"]
            if spec.get("name_attr"):
                attributes["name"] = spec["name_attr"]
            out.append(UIElement(element_id, spec["role"], spec["name"], value=str(spec.get("value", "")), attributes=attributes, bounds=(20, y, 160, 30)))
            y += 50
        return Observation("chrome", "Chrome", title, self.url, out, text=text)

    def screenshot(self, *, cancel: Any = None) -> bytes:
        return png(320, 480)

    def list_tabs(self, *, cancel: Any = None) -> list[dict[str, Any]]:
        return [{"id": "T1", "url": self.url, "title": "Shop", "active": True}]

    def wait_ready(self, *, cancel: Any = None, timeout: float = 30.0) -> None:
        pass

    def drain_journal(self) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------- act
    def _spec(self, element_id: str) -> dict[str, Any]:
        spec = self._ids.get(element_id)
        if spec is None:
            raise ElementNotFoundError(f"element {element_id} is gone")
        return spec

    def navigate(self, url: str, *, cancel: Any = None, reuse_tab: bool = False) -> Any:
        self.log.append(("navigate", url))
        self.url = url if "://" in url else f"{BASE}{url}"
        return None

    def click(self, element_id: str, *, cancel: Any = None) -> None:
        spec = self._spec(element_id)
        self.log.append(("click", spec["name"]))
        if spec.get("href"):
            self.url = BASE + spec["href"]
        elif spec["name"] in ("Export", "Download CSV"):
            self.state["exported"] = True
        elif spec["name"] == "Submit" and self.state.get("email"):
            self.state["submitted"] = True
        elif spec["name"] == "Go":
            self.state["query"] = self.state.get("typed_query", "")
        elif spec["name"] == "Delete account":
            self.state["deleted"] = True

    def right_click(self, element_id: str, *, cancel: Any = None) -> None:
        self.log.append(("right_click", self._spec(element_id)["name"]))

    def type_text(self, element_id: str, text: str, *, cancel: Any = None) -> None:
        spec = self._spec(element_id)
        self.log.append(("type", (spec["name"], text)))
        if spec["name"] == "Email" or spec.get("type") == "email":
            self.state["email"] = text
        if spec["name"] == "Search":
            self.state["typed_query"] = text
        if spec["name"] == "Password":
            self.state["password"] = text

    def press(self, key: str, *, cancel: Any = None) -> None:
        self.log.append(("press", key))

    def scroll(self, direction: str, *, cancel: Any = None) -> None:
        self.log.append(("scroll", direction))
        self.scrolled = True

    def select(self, element_id: str, option: str, *, cancel: Any = None) -> None:
        self.log.append(("select", option))

    def go_back(self, *, cancel: Any = None) -> str:
        self.url = f"{BASE}/"
        return self.url

    def pointer(self, kind: str, x: float, y: float, **kw: Any) -> None:
        button = kw.get("button") or "left"
        self.log.append(("pointer", (kind, x, y) if button == "left" else (kind, x, y, button)))

    def insert_text(self, text: str, **kw: Any) -> None:
        self.log.append(("insert_text", text))

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)
