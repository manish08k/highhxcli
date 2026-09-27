"""Computer-use runtime, candidates, safety, verification, recovery, flows, intents and the
DevTools transport."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highhx.commands import App
from highhx.computer.browser import CDPConnection
from highhx.computer.flows import FlowRunner, load_flow
from highhx.computer.intents import parse
from highhx.computer.model import Selector
from highhx.computer.runtime import ComputerRuntime, InvalidActionError
from highhx.computer.websocket import WebSocket
from highhx.core.context import Options
from highhx.core.errors import (
    ApprovalDeniedError,
    IntegrationError,
    NotFoundError,
    OperationCancelledError,
    PolicyViolationError,
    ValidationError,
)
from highhx.execution.cancellation import CancellationToken
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI
from tests.unit.computer.conftest import FakeShop


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    application = App(Options(interactive=True), cwd=tmp_path)
    yield application
    application.close()


def runtime(
    app: App,
    shop: FakeShop,
    *,
    actor: Actor = Actor.AGENT,
    ui: RecordingUI | None = None,
    mode: ApprovalMode = ApprovalMode.AUTO_EDIT,
) -> tuple[ComputerRuntime, RecordingUI]:
    ui = ui or RecordingUI()
    gate = ActionGate(app.engine, ui, source="computer", mode=mode, audit=AuditLog(app.db, app.redactor))
    return ComputerRuntime(shop, gate, actor=actor, settle=0), ui


# ----------------------------------------------------------------- candidates
def test_candidates_are_a_finite_valid_set(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    ids = {c.id for c in rt.candidates()}
    assert {"type:e1", "click:e2", "click:e5", "click:e6", "press:enter", "scroll:down", "done", "ask_user"} <= ids
    assert "click:e1" not in ids  # a text field is typed into, not clicked
    with pytest.raises(InvalidActionError):
        rt.act("click:e99")
    with pytest.raises(InvalidActionError):
        rt.act("run:rm -rf /")  # the model cannot invent primitives


def test_semantic_observation_hides_secret_values(app: App, shop: FakeShop) -> None:
    shop.controls[3].value = "hunter2"
    observation = ComputerRuntime(shop, ActionGate(app.engine, RecordingUI(), source="c"), actor=Actor.USER).observe()
    password = next(e for e in observation.elements if e.name == "Password")
    assert password.value == "" and password.secret and "secret" in password.label()
    assert 'button "Search"' in observation.summary()


# -------------------------------------------------------- execute + verify
def test_type_and_submit_are_verified(app: App, shop: FakeShop) -> None:
    rt, ui = runtime(app, shop)
    rt.observe()
    typed = rt.act("type:e1", "Adele")
    assert typed.ok and typed.verified is True
    submitted = rt.act("click:e2")  # a submit button: sensitive → confirmed
    assert ui.of("confirm_action") == ['Click button "Search"']
    assert submitted.verified and submitted.observation is not None and "Adele" in submitted.observation.text


def test_checkbox_toggle_is_verified(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    rt.observe()
    outcome = rt.act("click:e5")
    assert outcome.verified is True and shop.controls[4].checked is True


def test_dispatch_without_effect_is_not_reported_as_success(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    rt.observe()
    outcome = rt.act("click:e8")
    assert outcome.ok is False and outcome.verified is False and "no visible change" in outcome.problems[0]


def test_sensitive_click_declined_is_not_executed(app: App, shop: FakeShop) -> None:
    ui = RecordingUI(action_answers=[False])
    rt, _ = runtime(app, shop, ui=ui)
    rt.observe()
    with pytest.raises(ApprovalDeniedError):
        rt.act("click:e6")
    assert not shop.deleted and "click Delete account" not in shop.actions
    request = ui.requests[0]
    assert request.action == 'Click button "Delete account"' and "destructive" in " ".join(request.reasons)


def test_approval_does_not_carry_over_to_a_changed_ui(app: App, shop: FakeShop) -> None:
    """The user approved 'Delete account'; before it runs the page swaps that control out."""
    ui = RecordingUI()
    rt, _ = runtime(app, shop, ui=ui)
    rt.observe()
    original_confirm = ui.confirm_action

    def approve_then_change(request: Any) -> bool:
        shop.controls[5].visible = False  # the approved control disappears
        return original_confirm(request)

    ui.confirm_action = approve_then_change  # type: ignore[method-assign]
    with pytest.raises(NotFoundError, match="no longer on the screen"):
        rt.act("click:e6")
    assert not shop.deleted


def test_agent_cannot_type_passwords_but_the_user_can(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    rt.observe()
    with pytest.raises(PolicyViolationError, match="AI agent may not"):
        rt.act("type:e4", "hunter2")
    user_rt, _ = runtime(app, shop, actor=Actor.USER)
    user_rt.observe()
    outcome = user_rt.act("type:e4", "hunter2")
    assert outcome.verified is None  # secret values cannot be read back
    rows = "\n".join(str(r) for r in app.db.query("SELECT * FROM audit_log"))
    assert "hunter2" not in rows


def test_failed_sensitive_action_is_not_retried_automatically(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    shop.controls[5].on_click = None  # the delete button stops working
    rt.observe()
    first = rt.act("click:e6")
    assert first.ok is False
    rt.observe()
    with pytest.raises(IntegrationError, match="Not retrying automatically"):
        rt.act("click:e6")


def test_navigation_is_verified(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    assert rt.navigate("https://shop.test/help").verified is True
    shop.navigate = lambda url, cancel=None: None  # type: ignore[method-assign]  # the browser ignores it
    failed = rt.navigate("https://elsewhere.test/")
    assert failed.ok is False and "not https://elsewhere.test/" in failed.problems[0]


def test_cancellation_stops_before_acting(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop)
    rt.observe()
    rt.cancel = CancellationToken()
    rt.settle = 1.0
    threading.Timer(0.1, rt.cancel.cancel).start()
    with pytest.raises(OperationCancelledError):
        rt.act("type:e1", "x")


# ------------------------------------------------------ deterministic (Free)
def test_selectors_resolve_deterministically(app: App, shop: FakeShop) -> None:
    rt, _ = runtime(app, shop, actor=Actor.USER)
    assert rt.resolve(Selector.parse("button:Search")).id == "e2"
    assert rt.resolve(Selector.parse('textbox="Email"')).id == "e3"
    assert rt.resolve(Selector.parse("Search")).id == "e1"
    with pytest.raises(NotFoundError) as info:
        rt.resolve(Selector.parse("button:Serch"))
    assert "Did you mean: 'Search'" in (info.value.hint or "")


def test_flow_runs_with_discovery_recovery_and_expectations(
    app: App, shop: FakeShop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop.appear_after["Search"] = 3  # the search box renders late: the runner re-observes
    monkeypatch.setenv("SHOP_PASSWORD", "s3cr3t-value")
    flow_file = tmp_path / "flow.yaml"
    flow_file.write_text(
        "name: search\nsteps:\n"
        "  - open: https://shop.test/\n"
        "  - type: {into: 'searchbox:Search', text: Adele}\n"
        "  - press: enter\n"
        "  - expect: {text: 'You searched for Adele', url_contains: results}\n"
        "  - type: {into: 'textbox:Password', text_from_env: SHOP_PASSWORD}\n"
    )
    rt, _ = runtime(app, shop, actor=Actor.USER, mode=ApprovalMode.ASK)
    result = FlowRunner(rt, poll=0.01).run(load_flow(flow_file))
    assert result.ok, result.steps
    assert [s["ok"] for s in result.steps] == [True] * 5
    assert "s3cr3t-value" not in json.dumps(result.to_dict())


def test_flow_stops_at_the_first_failure(app: App, shop: FakeShop, tmp_path: Path) -> None:
    flow_file = tmp_path / "bad.yaml"
    flow_file.write_text("steps:\n  - click: 'button:Checkout'\n  - click: 'link:Help'\ntimeout: 0.05\n")
    rt, _ = runtime(app, shop, actor=Actor.USER)
    result = FlowRunner(rt, poll=0.01).run(load_flow(flow_file))
    assert not result.ok and len(result.steps) == 1 and "No element matches" in result.steps[0]["error"]


def test_flow_validation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("steps:\n  - click: x\n    type: y\n  - press: f13\n  - type: {into: x}\n")
    with pytest.raises(ValidationError) as info:
        load_flow(bad)
    assert len(info.value.details) == 3


@pytest.mark.parametrize(
    ("request_text", "kind", "detail"),
    [
        ("run the tests", "command", ["test"]),
        ("Please run all the tests.", "command", ["test"]),
        ("start the dev server", "command", ["dev"]),
        ("check", "command", ["check"]),
        ("Open Chrome and search for Adele", "search", "https://www.google.com/search?q=Adele"),
        ("open localhost:3000", "navigate", "http://localhost:3000"),
        ("open Calculator", "launch", "Calculator"),
    ],
)
def test_deterministic_intents(request_text: str, kind: str, detail: Any) -> None:
    intent = parse(request_text)
    assert intent is not None and intent.kind == kind
    assert detail in (intent.argv, intent.url, intent.app)


@pytest.mark.parametrize(
    "request_text",
    [
        "fix my failing tests",
        "run the tests and fix whatever is failing",
        "check this project for production issues",
        "open the deployment dashboard and check whether the deployment succeeded",
        "find the failing test, fix it, and rerun the test",
    ],
)
def test_open_ended_requests_need_the_agent(request_text: str) -> None:
    assert parse(request_text) is None


# ---------------------------------------------------------- DevTools transport
class FakeDevTools:
    """A minimal WebSocket server that speaks enough CDP for the client tests."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received: list[dict[str, Any]] = []
        self.stall = False
        self.drop: str | None = None
        """Drop the connection after receiving a command: "close" (FIN) or "reset" (RST, a crash)."""
        self.conn: socket.socket | None = None
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        for sock in (self.conn, self.server):
            if sock is not None:
                sock.close()

    def _send(self, conn: socket.socket, text: str, *, opcode: int = 1, fragment: bool = False) -> None:
        data = text.encode()
        if fragment:
            half = len(data) // 2
            conn.sendall(bytes([opcode, half]) + data[:half])  # FIN=0
            conn.sendall(bytes([0x80, len(data) - half]) + data[half:])  # continuation
            return
        header = bytes([0x80 | opcode])
        header += bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack("!H", len(data))
        conn.sendall(header + data)

    def _recv(self, conn: socket.socket) -> str:
        while True:
            opcode, text = self._frame(conn)
            if opcode == 1:
                return text  # skip the client's pong frames

    def _frame(self, conn: socket.socket) -> tuple[int, str]:
        head = conn.recv(2)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", conn.recv(2))[0]
        mask = conn.recv(4)
        payload = b""
        while len(payload) < length:
            payload += conn.recv(length - len(payload))
        return head[0] & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode()

    def _serve(self) -> None:
        conn, _ = self.server.accept()
        self.conn = conn
        request = conn.recv(4096).decode()
        key = next(
            line.split(":", 1)[1].strip()
            for line in request.split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        conn.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        while True:
            try:
                message = json.loads(self._recv(conn))
            except (OSError, ValueError, IndexError):
                return
            self.received.append(message)
            if self.drop:
                if self.drop == "reset":
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()
                return
            if self.stall:
                continue
            conn.sendall(bytes([0x89, 0]))  # a ping the client must answer transparently
            self._send(conn, json.dumps({"method": "Page.loadEventFired", "params": {}}))
            self._send(conn, json.dumps({"id": message["id"], "result": {"echo": message["method"]}}), fragment=True)


def test_cdp_connection_matches_responses_and_buffers_events() -> None:
    server = FakeDevTools()
    conn = CDPConnection(f"ws://127.0.0.1:{server.port}/devtools/page/1")
    assert conn.call("Runtime.evaluate", {"expression": "1"}) == {"echo": "Runtime.evaluate"}
    assert conn.call("Page.enable") == {"echo": "Page.enable"}
    assert [e["method"] for e in conn.events] == ["Page.loadEventFired", "Page.loadEventFired"]
    assert [m["id"] for m in server.received] == [1, 2]
    conn.close()
    server.close()


def test_cdp_calls_are_cancellable() -> None:
    server = FakeDevTools()
    server.stall = True
    conn = CDPConnection(f"ws://127.0.0.1:{server.port}/devtools/page/1")
    token = CancellationToken()
    threading.Timer(0.2, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelledError):
        conn.call("Runtime.evaluate", {"expression": "while(true){}"}, cancel=token)
    assert time.monotonic() - started < 2
    assert conn.ws.closed
    server.close()


def test_websocket_refuses_remote_endpoints() -> None:
    with pytest.raises(IntegrationError, match="non-local"):
        WebSocket("ws://example.com:9222/devtools/page/1")
    with pytest.raises(IntegrationError, match="ws://"):
        WebSocket("wss://127.0.0.1:9222/x")


def test_ocr_output_is_grouped_into_text_lines() -> None:
    from highhx.computer.desktop import parse_tesseract_tsv

    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [
        "5\t1\t1\t1\t1\t1\t10\t20\t40\t12\t96\tSign",
        "5\t1\t1\t1\t1\t2\t55\t20\t20\t12\t95\tin",
        "5\t1\t1\t1\t2\t1\t10\t40\t60\t12\t30\tnoise",  # low confidence: dropped
        "5\t1\t2\t1\t1\t1\t10\t80\t70\t14\t91\tPassword",
    ]
    observation = parse_tesseract_tsv("\n".join([header, *rows]))
    assert [(e.role, e.name) for e in observation.elements] == [("text", "Sign in"), ("text", "Password")]
    assert observation.elements[0].bounds == (10, 20, 65, 12) and observation.elements[0].source == "ocr"
