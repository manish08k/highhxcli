"""A small in-memory web for goal-loop tests: sites HighhX has never been configured for.

``FakeWeb`` is a browser provider (the same interface as the HighhX browser): pages with links,
forms, a search box, content below the fold, elements that render late, media that plays,
tabs and history — and scripted failures (a lost connection, an unknown outcome, a site that
does not resolve). None of its sites are in the target registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import pytest

from highhx.commands import App
from highhx.computer.browser import ElementNotFoundError, NavigationError
from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.computer.runtime import ComputerRuntime
from highhx.core.context import Options
from highhx.core.errors import IntegrationError
from highhx.execution.cancellation import CancellationToken
from highhx.goals.conditions import check
from highhx.goals.executor import GoalExecutor
from highhx.goals.ir import TaskIR
from highhx.goals.log import TaskLog
from highhx.goals.loop import GoalLoop
from highhx.goals.planner import ModelPlanner, Planner, ScriptedPlanner
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI


@dataclass
class Ctl:
    role: str
    name: str
    href: str = ""
    on_click: Callable[[FakeWeb], None] | None = None
    value: str = ""
    below_fold: bool = False
    appear_after: int = 0
    """Rendered only from this many observations of the page on (a slow page)."""
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class Page:
    title: str
    text: str
    controls: list[Ctl] = field(default_factory=list)
    media: bool = False
    """Opening this page by a click starts its media."""


def _search(web: FakeWeb) -> None:
    box = next(c for c in web.page().controls if c.role == "searchbox")
    origin = f"{urlparse(web.url).scheme}://{urlparse(web.url).hostname}"
    web.go(f"{origin}/search?q={quote_plus(box.value)}")


def default_pages() -> dict[str, Page]:
    return {
        "https://books.example/": Page(
            "Books Example",
            "The friendliest bookshop on the web.",
            [
                Ctl("searchbox", "Search books", attrs={"tag": "input", "type": "search"}),
                Ctl("button", "Search", on_click=_search, attrs={"tag": "button"}),
                Ctl("link", "Contact us", href="https://books.example/contact", attrs={"tag": "a"}),
                Ctl("link", "Weekly deals", href="https://books.example/deals", below_fold=True, attrs={"tag": "a"}),
            ],
        ),
        "https://books.example/contact": Page(
            "Contact — Books Example", "Questions? Write to hello@books.example or call +1 555 0100."
        ),
        "https://books.example/deals": Page("Deals — Books Example", "This week: 3 for 2 on all paperbacks."),
        "https://tube.example/": Page(
            "Tube",
            "Watch anything.",
            [
                Ctl("searchbox", "Search", attrs={"tag": "input", "type": "search"}),
                Ctl("button", "Search", on_click=_search, attrs={"tag": "button"}),
            ],
        ),
    }


class FakeWeb:
    name = "browser"

    def __init__(self, pages: dict[str, Page] | None = None) -> None:
        self.pages = pages or default_pages()
        self.tabs: dict[str, dict[str, Any]] = {"t1": {"history": ["about:blank"], "index": 0}}
        self._target_id = "t1"
        self.next_tab = 2
        self.views: dict[str, int] = {}
        self.scrolled = False
        self.playing = False
        self.calls: list[str] = []
        self.fail: dict[str, list[Exception]] = {}
        """op → exceptions the next calls of that op raise (a lost connection, an unknown outcome …)."""
        self.focused: str = ""

    # ------------------------------------------------------------ helpers
    @property
    def url(self) -> str:
        tab = self.tabs[self._target_id]
        return str(tab["history"][tab["index"]])

    def _op(self, op: str, detail: str = "") -> None:
        self.calls.append(f"{op} {detail}".strip())
        queue = self.fail.get(op)
        if queue:
            raise queue.pop(0)

    def page(self) -> Page:
        url = self.url
        if url == "about:blank":
            return Page("", "")
        if url in self.pages:
            return self.pages[url]
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}/"
        if parsed.path == "/search" and base in self.pages:
            query = parse_qs(parsed.query).get("q", [""])[0]
            if parsed.hostname == "tube.example":
                links = [
                    Ctl(
                        "link",
                        f"{query} — video {i}",
                        href=f"https://tube.example/watch?v={i}",
                        attrs={"tag": "a", "href": f"/watch?v={i}"},
                    )
                    for i in (1, 2)
                ]
                return Page(f"{query} - Tube", f"Results for {query}", links)
            return Page(
                f"Results for {query}",
                f"2 books match {query}",
                [Ctl("link", f"{query}: the novel", href="https://books.example/book/1", attrs={"tag": "a"})],
            )
        if parsed.hostname == "tube.example" and parsed.path == "/watch":
            return Page("Now playing - Tube", "Now playing", media=True)
        if parsed.hostname == "books.example":
            return Page("Book — Books Example", "A book.")
        return Page("404", "Not found")

    def go(self, url: str, *, clicked: bool = False) -> None:
        tab = self.tabs[self._target_id]
        del tab["history"][tab["index"] + 1 :]
        tab["history"].append(url)
        tab["index"] += 1
        self.scrolled = False
        self.playing = clicked and self.page().media

    def _visible(self) -> list[tuple[int, Ctl]]:
        views = self.views.get(self.url, 0)
        return [
            (i, c)
            for i, c in enumerate(self.page().controls)
            if (not c.below_fold or self.scrolled) and views >= c.appear_after
        ]

    def _control(self, element_id: str) -> Ctl:
        index = int(element_id[1:]) - 1
        for i, control in self._visible():
            if i == index:
                return control
        raise ElementNotFoundError(f"{element_id} is gone")

    # ------------------------------------------------------------ provider
    def capability(self) -> Capability:
        return Capability(self.name, True, "fake web")

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        self._op("observe")
        self.views[self.url] = self.views.get(self.url, 0) + 1
        page = self.page()
        elements = [
            UIElement(
                f"e{i + 1}",
                c.role,
                c.name,
                value=c.value,
                focused=self.focused == f"e{i + 1}",
                attributes={**c.attrs, **({"href": c.attrs.get("href", c.href)} if c.href else {})},
            )
            for i, c in self._visible()
        ]
        return Observation("browser", "FakeWeb", page.title, self.url, elements, page.text)

    def navigate(self, url: str, *, cancel: CancellationToken | None = None) -> None:
        self._op("navigate", url)
        host = urlparse(url).hostname or ""
        if not host.endswith(".example"):
            raise NavigationError(f"Could not open {url}: net::ERR_NAME_NOT_RESOLVED")
        self.go(url)

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        control = self._control(element_id)
        self._op("click", control.name)
        self.focused = element_id
        if control.href:
            self.go(control.href, clicked=True)
        elif control.on_click is not None:
            control.on_click(self)

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        control = self._control(element_id)
        self._op("type", control.name)
        self.focused = element_id
        control.value = text

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        self._op("press", key)
        if key == "enter" and self.focused:
            control = self._control(self.focused)
            if control.role == "searchbox":
                _search(self)

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        self._op("scroll", direction)
        self.scrolled = direction == "down"

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self._control(element_id).value = option

    def evaluate(self, expression: str, *, cancel: CancellationToken | None = None, retry_safe: bool = False) -> Any:
        self._op("evaluate")
        return self.playing

    def screenshot(self, *, cancel: CancellationToken | None = None) -> bytes:
        self._op("screenshot")
        return b"\x89PNG fake"

    def go_back(self, *, cancel: CancellationToken | None = None) -> str:
        self._op("back")
        tab = self.tabs[self._target_id]
        tab["index"] = max(0, tab["index"] - 1)
        return self.url

    def go_forward(self, *, cancel: CancellationToken | None = None) -> str:
        self._op("forward")
        tab = self.tabs[self._target_id]
        tab["index"] = min(len(tab["history"]) - 1, tab["index"] + 1)
        return self.url

    def reload(self, *, cancel: CancellationToken | None = None) -> None:
        self._op("reload")

    def new_tab(self, url: str | None = None, *, cancel: CancellationToken | None = None) -> Any:
        self._op("new_tab", url or "")
        tab = f"t{self.next_tab}"
        self.next_tab += 1
        self.tabs[tab] = {"history": ["about:blank"], "index": 0}
        self._target_id = tab
        if url:
            self.go(url)
        return None

    def close_tab(self, *, cancel: CancellationToken | None = None) -> dict[str, Any] | None:
        self._op("close_tab")
        del self.tabs[self._target_id]
        if not self.tabs:
            self.tabs["t1"] = {"history": ["about:blank"], "index": 0}
        self._target_id = next(iter(self.tabs))
        return {"url": self.url}

    def switch_tab(self, target: str, *, cancel: CancellationToken | None = None) -> dict[str, Any]:
        self._op("switch_tab", target)
        for tab, data in self.tabs.items():
            if target in str(data["history"][data["index"]]):
                self._target_id = tab
                return {"url": self.url, "id": tab}
        raise IntegrationError(f"No tab matches {target!r}.")


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    application = App(Options(interactive=True), cwd=tmp_path)
    yield application
    application.close()


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@dataclass
class Kit:
    runtime: ComputerRuntime
    executor: GoalExecutor
    ui: RecordingUI
    log: TaskLog
    app: App

    def loop(self, task: TaskIR, planner: Planner | None = None, *, replanner: ModelPlanner | None = None) -> GoalLoop:
        chosen = planner or ScriptedPlanner(task, lambda c, o: check(c, o, self.runtime))
        return GoalLoop(self.executor, chosen, self.log, replanner=replanner)

    def run(self, task: TaskIR, planner: Planner | None = None, *, replanner: ModelPlanner | None = None) -> Any:
        return self.loop(task, planner, replanner=replanner).run(task)

    def audit(self) -> list[dict[str, Any]]:
        rows = self.app.db.query("SELECT action, status FROM audit_log ORDER BY created_at, rowid")
        return [{"action": r["action"], "status": r["status"]} for r in rows]


def make_kit(app: App, provider: Any, *, actor: Actor = Actor.USER, ui: RecordingUI | None = None) -> Kit:
    ui = ui or RecordingUI()
    gate = ActionGate(
        app.engine, ui, source="computer", mode=ApprovalMode.AUTO_EDIT, audit=AuditLog(app.db, app.redactor)
    )
    runtime = ComputerRuntime(provider, gate, actor=actor, settle=0)
    executor = GoalExecutor(runtime, poll=0.01)
    return Kit(runtime, executor, ui, TaskLog(), app)


@pytest.fixture
def kit(app: App, web: FakeWeb) -> Kit:
    return make_kit(app, web)


from tests.unit.computer.test_browser_recovery import browser, chrome  # noqa: E402, F401  (fixtures)
