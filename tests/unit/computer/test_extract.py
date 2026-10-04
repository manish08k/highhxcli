"""Structured extraction (browser.extract with a JSON Schema): explicit labels, tables and lists
mapped onto the schema; ambiguity, bad values and missing required fields are reported, never
guessed; secret fields are never read. The real-Chrome test is opt-in: HIGHHX_TEST_BROWSER=1."""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.computer.browser import find_browser
from highhx.computer.extract import check_schema, convert, extract
from highhx.computer.session import ComputerSession
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI

ORDER = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string"},
        "total": {"type": "number"},
        "quantity": {"type": "integer"},
        "gift": {"type": "boolean"},
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "number"}}},
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["order_id", "total"],
}
PAGE: dict[str, Any] = {
    "url": "https://shop.test/orders/7",
    "title": "Order 7",
    "pairs": [
        ["Order ID", "A-1007", "dl"],
        ["Order total", "$1,234.50", "dl"],
        ["Subtotal", "$1,200.00", "dl"],
        ["Quantity", "3", "label"],
        ["Gift wrap", "yes", "label"],
    ],
    "tables": [
        {"caption": "Items", "headers": ["Name", "Price"], "rows": [["Lamp", "$34.50"], ["Desk", "1,200"]]},
    ],
    "lists": [{"label": "Tags", "items": ["home", "office"]}],
}


def test_a_page_is_mapped_onto_the_schema() -> None:
    found = extract(ORDER, PAGE)
    assert found.ok, (found.problems, found.missing)
    assert found.data == {
        "order_id": "A-1007",
        "total": 1234.5,  # "Order total", not "Subtotal": every term of the property must be in the label
        "quantity": 3,
        "gift": True,  # {"gift"} ⊆ {"gift", "wrap"}; "yes" converted to a boolean
        "items": [{"name": "Lamp", "price": 34.5}, {"name": "Desk", "price": 1200.0}],
        "tags": ["home", "office"],
    }
    assert found.sources == {
        "order_id": "dl",
        "total": "dl",
        "quantity": "label",
        "gift": "label",
        "items": "table",
        "tags": "list",
    }


def test_ambiguity_bad_values_and_missing_required_fields_are_reported() -> None:
    page = {
        "pairs": [["Total", "$5.00", "dl"], ["Total", "$7.00", "text"], ["Quantity", "a few", "label"]],
        "tables": [],
        "lists": [],
    }
    found = extract(ORDER, page)
    assert not found.ok
    assert "total" not in found.data and any(p.startswith("total: ambiguous") for p in found.problems)
    assert any(p.startswith("quantity:") and "not a single number" in p for p in found.problems)
    assert found.missing == ["order_id"]  # total is reported as ambiguous, not as missing
    same_twice = extract(
        ORDER, {"pairs": [["Order ID", "A-1", "dl"], ["Order ID", "A-1", "text"], ["Total", "2", "dl"]]}
    )
    assert same_twice.ok and same_twice.data["order_id"] == "A-1"  # the same value twice is not ambiguous


def test_values_are_converted_strictly() -> None:
    assert convert("€ 1.234", "number") == (1.234, "")
    assert convert("1,234", "integer") == (1234, "")
    assert convert("2.5", "integer")[0] is None
    assert convert("3 of 5", "number")[0] is None  # two numbers: which one? not guessed
    assert convert("Yes", "boolean") == (True, "") and convert("off", "boolean") == (False, "")
    assert convert("maybe", "boolean")[0] is None


def test_unsupported_schemas_are_refused() -> None:
    assert check_schema(ORDER) == []
    assert check_schema({"type": "object"}) == ["schema: an object needs properties"]
    assert check_schema({"type": "object", "properties": {"a": {"type": "date"}}})
    assert check_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
        }
    )
    assert check_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]})


def test_nested_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "shipping": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
        },
    }
    assert extract(schema, {"pairs": [["City", "Lyon", "label"]]}).data == {"shipping": {"city": "Lyon"}}
    missing = extract(schema, {"pairs": []})
    assert missing.missing == ["shipping.city"] and not missing.ok


SITE = """<!doctype html><title>Order 7</title>
<dl><dt>Order ID</dt><dd>A-1007</dd><dt>Order total</dt><dd>$1,234.50</dd><dt>Subtotal</dt><dd>$1,200.00</dd></dl>
<label for=q>Quantity</label><input id=q value="3">
<label><input type=checkbox checked> Gift wrap</label>
<label for=pw>Password</label><input id=pw type=password value="hunter2-SECRET">
<label for=cc>Card number</label><input id=cc autocomplete="cc-number" value="4111111111111111">
<input type=hidden name=csrf value="csrf-SECRET">
<table><caption>Items</caption><thead><tr><th>Name</th><th>Price</th></tr></thead>
<tbody><tr><td>Lamp</td><td>$34.50</td></tr><tr><td>Desk</td><td>1,200</td></tr></tbody></table>
<h3>Tags</h3><ul><li>home</li><li>office</li></ul>
<p>Status: shipped</p>
"""


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(SITE.encode())

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)
def test_schema_extraction_in_a_real_browser(agent_project: Path, make_app, tmp_path: Path) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state", headless=True)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    schema = json.loads(json.dumps(ORDER))
    schema["properties"].update(
        {"status": {"type": "string"}, "password": {"type": "string"}, "card_number": {"type": "string"}}
    )
    try:
        result = executor.run(
            "browser.extract", {"url": f"http://127.0.0.1:{server.server_address[1]}/", "schema": schema}
        )
        assert result.ok, result.error
        data = result.output["data"]
        assert data["order_id"] == "A-1007" and data["total"] == 1234.5 and data["quantity"] == 3
        assert data["gift"] is True and data["status"] == "shipped"
        assert data["items"] == [{"name": "Lamp", "price": 34.5}, {"name": "Desk", "price": 1200.0}]
        assert data["tags"] == ["home", "office"]
        assert "password" not in data and "card_number" not in data  # secret fields are never read
        dumped = json.dumps(result.output)
        assert "hunter2" not in dumped and "4111" not in dumped and "csrf-SECRET" not in dumped
        failing = executor.run(
            "browser.extract",
            {
                "schema": {
                    "type": "object",
                    "properties": {"invoice_number": {"type": "string"}},
                    "required": ["invoice_number"],
                }
            },
        )
        assert not failing.ok and failing.output["missing"] == ["invoice_number"]
    finally:
        if session._browser is not None:
            session._browser.stop()
        session.close()
        server.shutdown()
        server.server_close()
