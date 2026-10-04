"""The tab registry: a tab "shows" the address HighhX opened in it only while it is still on the
page that request landed on (redirects count). Once the page moves elsewhere — a person's click,
a script — "open <that address>" must navigate again, not believe it is already there."""

from __future__ import annotations

from highhx.computer.tabs import TabRegistry


def info(url: str, target: str = "T1") -> dict[str, str]:
    return {"targetId": target, "type": "page", "url": url, "title": ""}


def test_a_redirect_still_counts_as_the_requested_page() -> None:
    tabs = TabRegistry()
    tab = tabs.update(info("https://example.com/login?next=/"))
    assert tab is not None
    tab.opened("https://example.com/", "https://example.com/login?next=/")
    assert tabs.find("https://example.com/") is tab  # where "open example.com" landed


def test_a_later_navigation_clears_the_old_request() -> None:
    tabs = TabRegistry()
    tab = tabs.update(info("http://shop.test/index.html"))
    assert tab is not None
    tab.opened("http://shop.test/index.html", "http://shop.test/index.html")
    tabs.update(info("http://shop.test/invoices.html"))  # a click navigated the tab
    assert tabs.find("http://shop.test/index.html") is None
    assert tab.requested == "" and tabs.find("http://shop.test/invoices.html") is tab


def test_coming_back_to_the_page_is_found_by_its_address() -> None:
    tabs = TabRegistry()
    tab = tabs.update(info("http://shop.test/a"))
    assert tab is not None
    tab.opened("http://shop.test/a", "http://shop.test/a")
    tabs.update(info("http://shop.test/b"))
    tabs.update(info("http://shop.test/a"))
    assert tabs.find("http://shop.test/a") is tab
