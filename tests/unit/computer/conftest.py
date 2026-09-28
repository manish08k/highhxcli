"""A deterministic in-memory UI for computer-use tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from highhx.computer.browser import ElementNotFoundError
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.execution.cancellation import CancellationToken
from tests.unit.actions.conftest import repo  # noqa: F401
from tests.unit.agent.conftest import agent_project  # noqa: F401
from tests.unit.computer.test_browser_recovery import browser, chrome  # noqa: F401


@dataclass
class FakeControl:
    role: str
    name: str
    value: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    checked: bool | None = None
    on_click: Any = None
    visible: bool = True


class FakeShop:
    """A tiny web app: a search form, a login form, a newsletter checkbox and a destructive button."""

    name = "browser"

    def __init__(self) -> None:
        self.url = "https://shop.test/"
        self.title = "Shop"
        self.text = "Welcome to the shop"
        self.actions: list[str] = []
        self.controls = [
            FakeControl(
                "searchbox",
                "Search",
                attributes={"tag": "input", "type": "search", "form": "True", "form_method": "get"},
            ),
            FakeControl(
                "button",
                "Search",
                attributes={"tag": "button", "type": "submit", "form": "True", "form_method": "get"},
                on_click=self._search,
            ),
            FakeControl(
                "textbox", "Email", attributes={"tag": "input", "type": "email", "form": "True", "form_method": "post"}
            ),
            FakeControl(
                "textbox",
                "Password",
                attributes={
                    "tag": "input",
                    "type": "password",
                    "form": "True",
                    "form_method": "post",
                    "form_has_password": "True",
                },
            ),
            FakeControl("checkbox", "Newsletter", checked=False, attributes={"tag": "input", "type": "checkbox"}),
            FakeControl(
                "button",
                "Delete account",
                attributes={"tag": "button", "type": "button", "class": "btn btn-danger"},
                on_click=self._delete,
            ),
            FakeControl("link", "Help", attributes={"tag": "a", "href": "/help"}),
            FakeControl("button", "Nothing", attributes={"tag": "button", "type": "button"}),
        ]
        self.focused: int | None = None
        self.deleted = False
        self.observations = 0
        self.appear_after: dict[str, int] = {}

    def capability(self) -> Capability:
        return Capability(self.name, True, "fake")

    def _search(self) -> None:
        query = self.controls[0].value
        self.url = f"https://shop.test/results?q={query}"
        self.title = "Results"
        self.text = f"You searched for {query}"

    def _delete(self) -> None:
        self.deleted = True
        self.text = "Account deleted"

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        self.observations += 1
        elements = []
        for index, control in enumerate(self.controls):
            if control.name in self.appear_after and self.observations < self.appear_after[control.name]:
                continue
            if not control.visible:
                continue
            secret = control.attributes.get("type") == "password"
            elements.append(
                UIElement(
                    f"e{index + 1}",
                    control.role,
                    control.name,
                    value="" if secret else control.value,
                    focused=self.focused == index,
                    checked=control.checked,
                    attributes=dict(control.attributes),
                )
            )
        return Observation("browser", "FakeBrowser", self.title, self.url, elements, self.text)

    def _control(self, element_id: str) -> FakeControl:
        index = int(element_id[1:]) - 1
        if not 0 <= index < len(self.controls) or not self.controls[index].visible:
            raise ElementNotFoundError(f"{element_id} is gone")
        self.focused = index
        return self.controls[index]

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        control = self._control(element_id)
        self.actions.append(f"click {control.name}")
        if control.checked is not None:
            control.checked = not control.checked
        if control.on_click:
            control.on_click()

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        control = self._control(element_id)
        self.actions.append(f"type {control.name}")
        control.value = text

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        self.actions.append(f"press {key}")
        if key == "enter" and self.focused == 0:
            self._search()

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        self.actions.append(f"scroll {direction}")

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self._control(element_id).value = option

    def navigate(self, url: str, *, cancel: CancellationToken | None = None) -> None:
        self.actions.append(f"navigate {url}")
        self.url = url
        self.title = "Opened"


@pytest.fixture
def shop() -> FakeShop:
    return FakeShop()
