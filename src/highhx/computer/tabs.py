"""The HighhX browser's tabs, kept current from the browser's own target events.

Each tab has a stable identity (its DevTools target id), its opener when a page opened it,
and when it was created and last used. Choosing a tab is always an explicit decision from
this registry — never "the first tab the browser lists".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class Tab:
    id: str
    url: str = ""
    title: str = ""
    opener: str = ""
    """The tab that opened this one (window.open, target=_blank), when the browser reports it."""
    created: float = field(default_factory=time.monotonic)
    last_active: float = 0.0
    """When HighhX last worked in this tab (0: never)."""
    requested: str = ""
    """The address HighhX last opened here (the tab may show where that redirected)."""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "opener": self.opener}


def same_document(requested: str, actual: str) -> bool:
    """``actual`` is ``requested`` (ignoring the fragment, a trailing slash and a ``www.``)."""
    if not actual:
        return False

    def norm(url: str) -> tuple[str, str, str, str]:
        parsed = urlparse(url.split("#", 1)[0])
        host = (parsed.hostname or "").lower().removeprefix("www.")
        return parsed.scheme.lower(), host, parsed.path.rstrip("/"), parsed.query

    return norm(requested) == norm(actual)


class TabRegistry:
    def __init__(self) -> None:
        self.tabs: dict[str, Tab] = {}

    def update(self, info: dict[str, Any]) -> Tab | None:
        """Apply a TargetInfo (from Target.getTargets or a Target.* event); pages only."""
        target = str(info.get("targetId") or info.get("id") or "")
        if not target or info.get("type") not in (None, "page"):
            return None
        tab = self.tabs.get(target)
        if tab is None:
            tab = self.tabs[target] = Tab(target, opener=str(info.get("openerId") or ""))
        tab.url = str(info.get("url") or tab.url)
        tab.title = str(info.get("title") or tab.title)
        if info.get("openerId"):
            tab.opener = str(info["openerId"])
        return tab

    def remove(self, target: str) -> Tab | None:
        return self.tabs.pop(target, None)

    def sync(self, infos: list[dict[str, Any]]) -> None:
        """Replace the view with the browser's full list (tabs that vanished are dropped)."""
        seen = set()
        for info in infos:
            tab = self.update(info)
            if tab is not None:
                seen.add(tab.id)
        for target in set(self.tabs) - seen:
            del self.tabs[target]

    def touch(self, target: str) -> None:
        if target in self.tabs:
            self.tabs[target].last_active = time.monotonic()

    def get(self, target: str) -> Tab | None:
        return self.tabs.get(target) if target else None

    def pages(self) -> list[Tab]:
        """Real web pages, the most recently used first (then the newest)."""
        usable = [t for t in self.tabs.values() if not t.url.startswith(("devtools://", "chrome-extension://"))]
        return sorted(usable, key=lambda t: (t.last_active, t.created), reverse=True)

    def best(self, *, exclude: set[str] | None = None) -> Tab | None:
        """The tab to continue in when the current one is gone: the one HighhX used most recently,
        preferring tabs no page opened (a popup is never picked over a tab someone chose)."""
        pages = [t for t in self.pages() if t.id not in (exclude or set())]
        own = [t for t in pages if not t.opener or t.last_active]
        choices = own or pages
        return choices[0] if choices else None

    def find(self, url: str) -> Tab | None:
        """A tab showing ``url`` — or where HighhX opened ``url`` and it redirected."""
        return next(
            (t for t in self.pages() if same_document(url, t.url) or (t.requested and same_document(url, t.requested))),
            None,
        )

    def opened_by(self, opener: str, *, since: set[str]) -> list[Tab]:
        """Tabs ``opener`` opened that did not exist in ``since``."""
        return [t for t in self.tabs.values() if t.id not in since and t.opener == opener]
