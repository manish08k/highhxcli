"""Model-call retries, circuit breaking, idempotency keys, cancellation, the session state
machine, session leases, protocol compatibility and cancellable platform streams."""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.history import SessionBusyError, SessionStore
from highhx.agent.model.resilience import CircuitBreaker, RetryPolicy
from highhx.agent.permissions import ApprovalMode
from highhx.agent.state import InvalidTransitionError, SessionState, can_transition, check_transition
from highhx.cloud import protocol
from highhx.cloud.client import PlatformClient
from highhx.core.errors import CloudError, ModelProviderError, OperationCancelledError
from highhx.execution.cancellation import CancellationToken
from tests.unit.agent.conftest import PY, RecordingUI, reply, retryable


# -------------------------------------------------------------- retry policy
def test_backoff_is_exponential_with_bounded_jitter() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=0.25)
    assert [policy.delay(n, rng=lambda: 0.5) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert policy.delay(3, rng=lambda: 0.0) == 3.0 and policy.delay(3, rng=lambda: 1.0) == 5.0


def test_circuit_breaker_opens_and_recovers() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=3, reset_after=30, clock=lambda: now[0])
    for _ in range(2):
        breaker.failure()
    breaker.check()
    breaker.failure()
    with pytest.raises(ModelProviderError, match="failing repeatedly") as info:
        breaker.check()
    assert not info.value.retryable and "Deterministic HighhX commands" in (info.value.hint or "")
    now[0] = 31  # half-open: one trial call allowed
    breaker.check()
    breaker.success()
    assert breaker.failures == 0 and not breaker.open


def test_circuit_breaker_is_thread_safe() -> None:
    breaker = CircuitBreaker(failure_threshold=10_000)
    threads = [threading.Thread(target=lambda: [breaker.failure() for _ in range(1000)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert breaker.failures == 8000


# ----------------------------------------------------------- session retries
def test_retryable_failures_retry_with_a_new_idempotency_key(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(agent_project, [retryable(), retryable(), reply("ok")])
    assert session.run_turn("hi").text == "ok"
    keys = [r.attempt_id for r in provider.requests]
    assert len(keys) == 3 and len(set(keys)) == 3 and all(keys)


def test_non_retryable_failures_are_not_retried(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(
        agent_project, [ModelProviderError("Anthropic rejected the API key.", status=401), reply("x")]
    )
    with pytest.raises(ModelProviderError, match="API key"):
        session.run_turn("hi")
    assert len(provider.requests) == 1
    assert session.state == SessionState.FAILED


def test_retry_attempts_are_bounded(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(agent_project, [retryable() for _ in range(10)])
    with pytest.raises(ModelProviderError):
        session.run_turn("hi")
    assert len(provider.requests) == session.retry.max_attempts


def test_malformed_stream_without_completion_is_retried(agent_project: Path, make_session) -> None:
    from highhx.agent.streaming import TextDelta

    session, provider, _ = make_session(agent_project, [[TextDelta("partial")], reply("complete")])
    assert session.run_turn("hi").text == "complete" and len(provider.requests) == 2


def test_circuit_opens_and_fails_fast(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(agent_project, [retryable() for _ in range(20)])
    session.breaker = CircuitBreaker(failure_threshold=3, reset_after=60)
    with pytest.raises(ModelProviderError):
        session.run_turn("first")
    calls = len(provider.requests)
    assert calls == 3
    with pytest.raises(ModelProviderError, match="failing repeatedly"):
        session.run_turn("second")
    assert len(provider.requests) == calls  # failed fast, no provider call


def test_cancellation_stops_the_retry_loop(agent_project: Path, make_session) -> None:
    session, provider, _ = make_session(agent_project, [retryable() for _ in range(10)])
    session.retry = RetryPolicy(base_delay=5.0, max_delay=5.0, jitter=0)
    token = CancellationToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    result = session.run_turn("hi", cancel=token)
    assert result.stopped == "cancelled" and time.monotonic() - started < 3
    assert len(provider.requests) == 1
    assert session.state == SessionState.CANCELLED


def test_model_retry_never_reexecutes_a_tool(agent_project: Path, make_session) -> None:
    counter = agent_project / "count.txt"
    command = f"{PY} -c \"open('count.txt', 'a').write('x')\""
    session, _provider, _ = make_session(
        agent_project,
        [reply("", [("run_command", {"command": command})]), retryable(), retryable(), reply("done")],
        mode=ApprovalMode.AUTO_EDIT,
    )
    result = session.run_turn("count once")
    assert result.text == "done"
    assert counter.read_text() == "x"


def test_failed_tool_is_not_automatically_rerun(agent_project: Path, make_session) -> None:
    session, _provider, _ = make_session(
        agent_project,
        [
            reply(
                "", [("run_command", {"command": f"{PY} -c \"open('runs.txt','a').write('x'); raise SystemExit(3)\""})]
            ),
            reply("it failed"),
        ],
        mode=ApprovalMode.AUTO_EDIT,
    )
    session.run_turn("run it")
    assert (agent_project / "runs.txt").read_text() == "x"


def test_running_command_is_killed_on_cancel(agent_project: Path, make_session) -> None:
    marker = agent_project / "started.txt"
    command = f"{PY} -c \"import time; open('started.txt','w').write('1'); time.sleep(60)\""
    session, _provider, _ = make_session(
        agent_project,
        [reply("", [("run_command", {"command": command, "timeout": 120})]), reply("x")],
        mode=ApprovalMode.AUTO_EDIT,
    )
    token = CancellationToken()

    def cancel_when_started() -> None:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        token.cancel("stop")

    threading.Thread(target=cancel_when_started, daemon=True).start()
    started = time.monotonic()
    result = session.run_turn("long", cancel=token)
    assert result.stopped == "cancelled" and time.monotonic() - started < 15


# ------------------------------------------------------------ state machine
def test_transition_table() -> None:
    S = SessionState
    assert can_transition(S.CREATED, S.RUNNING) and can_transition(S.RUNNING, S.WAITING_FOR_CONFIRMATION)
    assert can_transition(S.WAITING_FOR_CONFIRMATION, S.RUNNING) and can_transition(S.RUNNING, S.COMPLETED)
    assert can_transition(S.COMPLETED, S.CLOSED) and can_transition(S.CLOSED, S.RUNNING)
    for bad in (
        (S.CREATED, S.COMPLETED),
        (S.WAITING_FOR_CONFIRMATION, S.COMPLETED),
        (S.COMPLETED, S.WAITING_FOR_CONFIRMATION),
        (S.CLOSED, S.COMPLETED),
    ):
        with pytest.raises(InvalidTransitionError):
            check_transition(*bad)


def test_session_lifecycle_through_a_confirmation(agent_project: Path, make_session) -> None:
    states: list[str] = []
    ui = RecordingUI()
    session, _provider, _ = make_session(
        agent_project,
        [
            reply("", [("delete_file", {"path": "README.md"})])
            if (agent_project / "README.md").exists()
            else reply("", [("run_command", {"command": "rm -rf build"})]),
            reply("ok"),
        ],
        ui=ui,
    )
    original = ui.confirm_action

    def observe(request: Any) -> bool:
        states.append(str(session.state))
        return original(request)

    ui.confirm_action = observe  # type: ignore[method-assign]
    assert session.state == SessionState.CREATED
    session.run_turn("clean up")
    assert states == ["waiting_for_confirmation"]
    assert session.state == SessionState.COMPLETED and session.record.status == "completed"
    session.close()
    assert session.record.status == "closed"


def test_concurrent_turns_on_one_session_are_rejected(agent_project: Path, make_session) -> None:
    release = threading.Event()

    def slow(_request: Any) -> list[Any]:
        release.wait(10)
        return reply("slow")

    session, _provider, _ = make_session(agent_project, [slow, reply("x")])
    worker = threading.Thread(target=session.run_turn, args=("one",))
    worker.start()
    time.sleep(0.2)
    with pytest.raises(SessionBusyError):
        session.run_turn("two")
    release.set()
    worker.join(10)


def test_session_lease_blocks_other_processes_and_recovers_from_crashes(agent_project: Path, make_session) -> None:
    session, _provider, _ = make_session(agent_project, [reply("x")])
    store: SessionStore = session.store
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.db.execute(
            "UPDATE agent_sessions SET lease_owner = ? WHERE id = ?",
            (f"{socket.gethostname()}:{holder.pid}", session.record.id),
        )
        with pytest.raises(SessionBusyError, match="in use by another HighhX process"):
            session.run_turn("hi")
    finally:
        holder.kill()
        holder.wait()
    assert session.run_turn("hi").text == "x"  # the dead holder's lease is reclaimed
    session.close()
    assert (
        store.db.query_one("SELECT lease_owner FROM agent_sessions WHERE id = ?", (session.record.id,))["lease_owner"]
        is None
    )


# ------------------------------------------------------------------ protocol
def test_protocol_version_checks() -> None:
    assert protocol.check_client(protocol.PROTOCOL_VERSION) == (protocol.parse(protocol.PROTOCOL_VERSION), None)
    assert protocol.check_client("1.0")[1] is None
    assert protocol.check_client("2.3")[1] == "client_unsupported"
    assert protocol.check_client("0.4")[1] == "client_outdated"
    assert protocol.check_client("1")[1] == "client_unsupported"
    assert protocol.check_client(None)[1] == "client_unsupported"
    assert (
        protocol.compatible_server("1.7")
        and not protocol.compatible_server("2.0")
        and not protocol.compatible_server("x")
    )


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "ok"
    seen: list[dict[str, str]] = []

    def log_message(self, *args: Any) -> None:
        return None

    def do_GET(self) -> None:
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.mode == "outdated":
            payload = b'{"detail": {"code": "client_outdated", "message": "Too old", "hint": "Update with pip"}}'
            self.send_response(426)
        else:
            payload = b"{}"
            self.send_response(200)
        self.send_header(protocol.PROTOCOL_HEADER, "2.0" if self.mode == "future" else protocol.PROTOCOL_VERSION)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header(protocol.PROTOCOL_HEADER, protocol.PROTOCOL_VERSION)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'id: 1\nevent: text\ndata: {"text": "a"}\n\n')
        self.wfile.flush()
        time.sleep(30)  # a stalled upstream


@pytest.fixture
def fake_platform_server() -> Any:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Handler.seen = []
    yield server
    server.shutdown()
    server.server_close()


def test_client_sends_and_checks_protocol(fake_platform_server: Any) -> None:
    url = f"http://127.0.0.1:{fake_platform_server.server_address[1]}"
    client = PlatformClient(url, "hhx_t")
    _Handler.mode = "ok"
    client.get("/v1/me")
    assert _Handler.seen[-1][protocol.PROTOCOL_HEADER.lower()] == protocol.PROTOCOL_VERSION
    assert _Handler.seen[-1][protocol.CLIENT_HEADER.lower()].startswith("highhx-cli/")
    _Handler.mode = "future"
    with pytest.raises(CloudError, match=r"speaks protocol 2\.0"):
        client.get("/v1/me")
    _Handler.mode = "outdated"
    with pytest.raises(CloudError, match="Too old") as info:
        client.get("/v1/me")
    assert "pip" in (info.value.hint or "")


def test_stream_cancellation_closes_the_connection(fake_platform_server: Any) -> None:
    url = f"http://127.0.0.1:{fake_platform_server.server_address[1]}"
    token = CancellationToken()
    stream = PlatformClient(url, "hhx_t").stream("/v1/ai/messages", {}, cancel=token)
    first = next(stream)
    assert first.id == 1 and first.data == {"text": "a"}
    threading.Timer(0.2, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelledError):
        next(stream)
    assert time.monotonic() - started < 3
