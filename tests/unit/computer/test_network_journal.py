"""Network evidence from DevTools: sanitized (no credentials, query values, fragments, headers or
bodies), redirects and failures, ordered and bounded; fed by the browser's event handler."""

from __future__ import annotations

from pathlib import Path

from highhx.computer.browser import ChromeBrowser
from highhx.computer.network import NetworkJournal, sanitize_url


def sent(journal: NetworkJournal, rid: str, url: str, method: str = "GET", session: str = "S", **extra: object) -> None:
    journal.handle(
        "Network.requestWillBeSent",
        {
            "requestId": rid,
            "request": {
                "url": url,
                "method": method,
                "headers": {"Authorization": "Bearer xyz"},
                "postData": "password=hunter2",
            },
            "type": "Fetch",
            **extra,
        },
        session,
    )


def test_urls_keep_only_what_identifies_the_endpoint() -> None:
    assert (
        sanitize_url("https://user:pw@api.test:8443/v1/orders?token=abc&id=7#frag")
        == "https://api.test:8443/v1/orders?token=…&id=…"
    )
    assert sanitize_url("http://shop.test") == "http://shop.test/"


def test_requests_responses_failures_and_redirects() -> None:
    journal = NetworkJournal()
    start = journal.mark()
    sent(journal, "1", "https://api.test/orders?token=SECRET", "POST")
    journal.handle(
        "Network.responseReceived",
        {"requestId": "1", "response": {"status": 201, "headers": {"Set-Cookie": "s=1"}}},
        "S",
    )
    sent(journal, "2", "https://cdn.test/app.js")
    journal.handle("Network.loadingFailed", {"requestId": "2", "errorText": "net::ERR_NAME_NOT_RESOLVED"}, "S")
    sent(journal, "3", "http://old.test/a")
    sent(journal, "3", "https://new.test/a", redirectResponse={"status": 301})
    journal.handle("Network.responseReceived", {"requestId": "3", "response": {"status": 200}}, "S")
    sent(journal, "4", "data:image/png;base64,AAAA")
    sent(journal, "5", "https://slow.test/pending")  # never finished
    entries = journal.since(start)
    assert entries == [
        {"method": "POST", "url": "https://api.test/orders?token=…", "status": 201, "type": "Fetch"},
        {
            "method": "GET",
            "url": "https://cdn.test/app.js",
            "status": None,
            "type": "Fetch",
            "error": "net::ERR_NAME_NOT_RESOLVED",
        },
        {"method": "GET", "url": "http://old.test/a", "status": 301, "type": "Fetch"},
        {"method": "GET", "url": "https://new.test/a", "status": 200, "type": "Fetch"},
    ]
    text = str(entries)
    assert "SECRET" not in text and "hunter2" not in text and "Bearer" not in text and "Set-Cookie" not in text
    middle = journal.mark()
    sent(journal, "6", "https://api.test/later")
    journal.handle("Network.responseReceived", {"requestId": "6", "response": {"status": 204}}, "S")
    assert [e["url"] for e in journal.since(middle)] == ["https://api.test/later"]


def test_sessions_are_kept_apart_and_the_journal_is_bounded() -> None:
    journal = NetworkJournal(capacity=10)
    sent(journal, "1", "https://a.test/x", session="A")
    journal.handle("Network.responseReceived", {"requestId": "1", "response": {"status": 500}}, "B")  # another tab's id
    assert journal.since(0) == []
    for n in range(50):
        sent(journal, f"r{n}", f"https://a.test/{n}")
    assert len(journal) == 10


def test_the_browser_feeds_its_journal_from_devtools_events(tmp_path: Path) -> None:
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    mark = browser.network.mark()
    browser._on_event(
        {
            "method": "Network.requestWillBeSent",
            "sessionId": "S1",
            "params": {
                "requestId": "9",
                "request": {"url": "https://x.test/p?q=1", "method": "GET"},
                "type": "Document",
            },
        }
    )
    browser._on_event(
        {
            "method": "Network.responseReceived",
            "sessionId": "S1",
            "params": {"requestId": "9", "response": {"status": 404}},
        }
    )
    assert browser.network.since(mark) == [
        {"method": "GET", "url": "https://x.test/p?q=…", "status": 404, "type": "Document"}
    ]
