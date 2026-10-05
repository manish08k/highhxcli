"""api.request hardening (this phase): SSRF protection decided on the address actually dialled
(redirects included), credentials never handed to another host, private networks only with
allow_private, plus query/form bodies, retries, extraction and response-schema validation.

Regression: plain urllib forwarded ``Authorization`` to whatever host a redirect named, and followed
redirects into this computer, private networks and cloud metadata."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.handlers import api
from tests.unit.actions.conftest import executor_for  # noqa: F401
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


class Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, Any]]
    script: dict[str, Any]

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.seen.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
                "body": body,
                "type": self.headers.get("Content-Type"),
            }
        )
        action = self.script.get(self.path.split("?")[0], {"status": 200, "json": {"ok": True}})
        if callable(action):
            action = action(len(self.seen))
        self.send_response(action["status"])
        if "location" in action:
            self.send_header("Location", action["location"])
        payload = json.dumps(action.get("json", {})).encode() if "raw" not in action else action["raw"]
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _handle

    def log_message(self, *args: Any) -> None:
        pass


STARTED: list[http.server.ThreadingHTTPServer] = []


def server(script: dict[str, Any]) -> tuple[http.server.ThreadingHTTPServer, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    handler = type("H", (Recorder,), {"seen": seen, "script": script})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STARTED.append(srv)
    return srv, seen


@pytest.fixture(autouse=True)
def close_servers() -> Iterator[None]:
    yield
    while STARTED:
        srv = STARTED.pop()
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def public_names(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hosts *.public.test resolve to 127.0.0.1 and count as public internet addresses."""
    real_resolve, real_classify = api.resolve, api.classify_address
    monkeypatch.setattr(
        api, "resolve", lambda host, port: ["127.0.0.1"] if host.endswith(".public.test") else real_resolve(host, port)
    )
    state = {"public": True}
    monkeypatch.setattr(
        api, "classify_address", lambda ip: "public" if ip == "127.0.0.1" and state["public"] else real_classify(ip)
    )
    yield


def test_credentials_never_follow_a_redirect(agent_project, executor_for, monkeypatch, public_names) -> None:  # noqa: F811
    other, other_seen = server({})
    first, _ = server(
        {"/start": {"status": 302, "location": f"http://other.public.test:{other.server_address[1]}/landing"}}
    )
    monkeypatch.setenv("API_TOKEN", "tok-SSRF-1")
    executor, _ = executor_for(agent_project)
    result = executor.run(
        "api.request",
        {
            "url": f"http://api.public.test:{first.server_address[1]}/start",
            "headers_from_env": {"Authorization": "API_TOKEN"},
            "headers": {"Cookie": "s=1"},
        },
    )
    assert result.ok and result.output["redirects"] == [f"http://other.public.test:{other.server_address[1]}/landing"]
    assert other_seen[0]["auth"] is None and other_seen[0]["cookie"] is None  # never handed to the other host


def test_redirects_into_private_space_are_refused(agent_project, executor_for, public_names) -> None:  # noqa: F811
    executor, _ = executor_for(agent_project)
    for location in (
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:1/admin",
        "http://10.0.0.8/internal",
        "file:///etc/passwd",
    ):
        bouncer, _ = server({"/go": {"status": 302, "location": location}})
        result = executor.run("api.request", {"url": f"http://site.public.test:{bouncer.server_address[1]}/go"})
        assert not result.ok and ("refused" in result.error or "was not followed" in result.error), (
            location,
            result.error,
        )


def test_direct_requests_to_metadata_and_private_networks(agent_project: Path, executor_for) -> None:  # noqa: F811
    executor, _ = executor_for(agent_project)
    metadata = executor.run("api.request", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert not metadata.ok and "link-local" in metadata.error
    private = executor.run("api.request", {"url": "http://10.255.255.1:9/"})
    assert not private.ok and "allow_private" in private.error
    assert executor.plan("api.request", {"url": "http://10.255.255.1/", "allow_private": True}).decision.risk == 3
    assert api.classify_address("::ffff:169.254.169.254") == "link_local"
    assert api.classify_address("0.0.0.0") == "reserved" and api.classify_address("192.168.1.4") == "private"
    assert api.classify_address("93.184.216.34") == "public"


def test_this_computer_is_allowed_when_asked_for(agent_project: Path, executor_for) -> None:  # noqa: F811
    local, seen = server({"/ok": {"status": 200, "json": {"data": {"items": [{"id": 7}]}}}})
    executor, _ = executor_for(agent_project)
    result = executor.run(
        "api.request",
        {
            "url": f"http://127.0.0.1:{local.server_address[1]}/ok?a=1",
            "query": {"b": "two words"},
            "extract": {"first_id": "data.items.0.id"},
            "response_schema": {"type": "object", "required": ["data"], "properties": {"data": {"type": "object"}}},
        },
    )
    assert result.ok and result.output["extracted"] == {"first_id": 7} and result.output["schema_problems"] == []
    assert seen[0]["path"] == "/ok?a=1&b=two+words"
    failing = executor.run(
        "api.request",
        {
            "url": f"http://127.0.0.1:{local.server_address[1]}/ok",
            "response_schema": {"type": "object", "required": ["missing"]},
        },
    )
    assert not failing.ok and "$.missing: required" in failing.error
    absent = executor.run(
        "api.request", {"url": f"http://127.0.0.1:{local.server_address[1]}/ok", "extract": {"x": "data.nope"}}
    )
    assert not absent.ok and "extract x" in absent.error


def test_forms_retries_and_the_size_limit(agent_project: Path, executor_for) -> None:  # noqa: F811
    flaky, seen = server(
        {
            "/flaky": lambda n: {"status": 503} if n < 3 else {"status": 200, "json": {"ok": 1}},
            "/big": {"status": 200, "raw": b"x" * 100_000},
        }
    )
    executor, _ui = executor_for(agent_project)
    ok = executor.run("api.request", {"url": f"http://127.0.0.1:{flaky.server_address[1]}/flaky", "retries": 3})
    assert ok.ok and len(seen) == 3  # two 503s, then success
    big = executor.run("api.request", {"url": f"http://127.0.0.1:{flaky.server_address[1]}/big"})
    assert (
        big.output["truncated"] is True and len(big.output["body"]) == 64_000
    )  # regression: the e-mail limit shadowed it
    posted = executor.run(
        "api.request",
        {"url": f"http://127.0.0.1:{flaky.server_address[1]}/form", "method": "POST", "form": {"q": "a&b", "n": "1"}},
    )
    assert posted.ok and seen[-1]["body"] == "q=a%26b&n=1" and seen[-1]["type"] == "application/x-www-form-urlencoded"
    before = len(seen)
    executor.run(
        "api.request",
        {"url": f"http://127.0.0.1:{flaky.server_address[1]}/flaky", "method": "POST", "retries": 3, "json": {}},
    )
    assert len(seen) == before + 1  # POST is never retried
