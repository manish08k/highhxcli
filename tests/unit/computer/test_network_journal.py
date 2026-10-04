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
        {"method": "GET", "url": "http://old.test/a", "status": 301, "type": "Fetch", "redirect": True},
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


def test_timing_in_flight_requests_and_matching() -> None:
    journal = NetworkJournal()
    start = journal.mark()
    sent(journal, "1", "https://api.test/orders?id=9", "POST", timestamp=100.0)
    assert journal.inflight() == 1
    journal.handle(
        "Network.responseReceived", {"requestId": "1", "timestamp": 100.120, "response": {"status": 201}}, "S"
    )
    journal.handle("Network.loadingFinished", {"requestId": "1", "timestamp": 100.250}, "S")
    assert journal.inflight() == 0
    sent(journal, "2", "http://a.test/", timestamp=200.0)
    sent(journal, "2", "https://a.test/", redirectResponse={"status": 308}, timestamp=200.040)
    journal.handle("Network.responseReceived", {"requestId": "2", "timestamp": 200.1, "response": {"status": 200}}, "S")
    entries = journal.since(start)
    assert entries[0] == {
        "method": "POST",
        "url": "https://api.test/orders?id=…",
        "status": 201,
        "type": "Fetch",
        "ms": 250,
    }
    assert entries[1]["redirect"] is True and entries[1]["ms"] == 40 and entries[1]["status"] == 308
    assert entries[2]["ms"] == 60 and "redirect" not in entries[2]
    assert journal.find(start, url_contains="/orders", method="post", status=201) == entries[0]
    assert journal.find(start, url_contains="/orders", status=500) is None
    assert journal.find(start, url_contains="id=9") is None  # query values are never kept, so never matched
    # a response without timestamps has no duration rather than a made-up one
    sent(journal, "3", "https://b.test/")
    journal.handle("Network.responseReceived", {"requestId": "3", "response": {"status": 200}}, "S")
    assert "ms" not in journal.since(start)[-1]
    # unrelated Network events do not count as activity
    before = journal.last_activity
    journal.handle("Network.dataReceived", {"requestId": "3"}, "S")
    assert journal.last_activity == before


def test_network_waits_without_a_journal_fail_instead_of_guessing() -> None:
    from types import SimpleNamespace

    from highhx.actions.handlers.native import browser_wait
    from highhx.execution.cancellation import CancellationToken

    simulated = SimpleNamespace(browser=SimpleNamespace())  # a browser that reports no network
    ctx = SimpleNamespace(computer=lambda: simulated, cancel=CancellationToken())
    for inputs in ({"network_idle": True}, {"request": {"url_contains": "/api"}}):
        result = browser_wait(ctx, inputs)  # type: ignore[arg-type]
        assert not result.ok and "network evidence is not available" in result.error


def test_waiting_for_a_request_that_finished_just_before_the_wait() -> None:
    """The click that sent it ran first; a finished request within the look-back still counts."""
    from types import SimpleNamespace

    from highhx.actions.handlers.native import browser_wait
    from highhx.execution.cancellation import CancellationToken

    journal = NetworkJournal()
    sent(journal, "1", "https://api.test/orders?k=v", "POST")
    journal.handle("Network.responseReceived", {"requestId": "1", "response": {"status": 201}}, "S")
    browser = SimpleNamespace(network=journal, _conn=object(), pump_events=lambda seconds, cancel=None: None)
    ctx = SimpleNamespace(computer=lambda: SimpleNamespace(browser=browser), cancel=CancellationToken())
    found = browser_wait(ctx, {"request": {"url_contains": "/orders", "status": 201}, "timeout": 1})  # type: ignore[arg-type]
    assert found.ok and found.output["network"][0]["url"] == "https://api.test/orders?k=…"
    idle = browser_wait(ctx, {"network_idle": True, "idle_ms": 50, "timeout": 2})  # type: ignore[arg-type]
    assert idle.ok
    sent(journal, "2", "https://api.test/slow")  # in flight for good
    stuck = browser_wait(ctx, {"network_idle": True, "timeout": 0.3})  # type: ignore[arg-type]
    assert not stuck.ok and "1 in flight" in stuck.error
