"""The HighhX web console: a browser view of the same runtime the CLI and TUI use.

    highhx web            →  http://127.0.0.1:PORT/?token=…   (printed once; loopback only)

What it shows — tasks and their history, live state, the plan, actions and tool calls, network
evidence, the trajectory and its event timeline, logs, approvals waiting for you, artifacts,
benchmarks, capabilities, and a live view of the browser or desktop — all read from the canonical
event bus and stores. What it does — start a task, pause, resume or cancel it, and answer
approvals (approve, reject, modify, defer) — goes through the one agent loop and executor: risk,
policy, approval, audit and trajectories exactly as on the command line.

Security: bound to 127.0.0.1 only; every request needs the random per-run token (constant-time
comparison); the Host header must be the loopback address (DNS rebinding); state-changing requests
need the token in an ``X-HighhX-Token`` header and a same-origin ``Origin`` (CSRF); responses carry
a strict Content-Security-Policy. Event payloads are redacted before they reach the bus; frames are
never stored.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from highhx.approvals.queue import ApprovalQueue, QueuePrompter
from highhx.computer.livestream import BrowserScreencast, FrameBroker, ScreenshotPoller
from highhx.core.errors import UsageError

if TYPE_CHECKING:
    from highhx.commands import App

MAX_BODY = 256_000


@dataclass
class WebTask:
    id: str
    goal: str
    surface: str
    status: str = "starting"
    summary: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    trace_id: str = ""
    cancel: Any = None
    pause: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "surface": self.surface,
            "status": "paused" if self.pause.is_set() and self.status == "running" else self.status,
            "summary": self.summary,
            "started": self.started,
            "ended": self.ended,
            "trace_id": self.trace_id,
        }


class WebConsole:
    def __init__(
        self, app: App, *, port: int = 0, token: str | None = None, headless: bool | None = None, max_fps: float = 5.0
    ) -> None:
        from highhx.observability.stream import EventRecorder

        self.app = app
        self.token = token or secrets.token_urlsafe(24)
        self.recorder = EventRecorder.attach(app.ctx.events, redactor=app.redactor)
        self.approvals = ApprovalQueue(emit=app.ctx.events.emit)
        self.prompter = QueuePrompter(self.approvals)
        self.headless = headless
        self.tasks: dict[str, WebTask] = {}
        self.brokers = {"browser": FrameBroker(max_fps=max_fps), "desktop": FrameBroker(max_fps=min(max_fps, 2.0))}
        self.streamers: dict[str, Any] = {}
        self._session: Any = None
        self._caps: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"console": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, name="highhx-web", daemon=True)
        self._thread.start()

    def close(self) -> None:
        for streamer in self.streamers.values():
            streamer.stop()
        for task in list(self.tasks.values()):
            if task.cancel is not None:
                task.cancel.cancel("console closed")
        self.server.shutdown()
        self.server.server_close()
        if self._session is not None:
            self._session.close()

    # ------------------------------------------------------------- execution
    def session(self) -> Any:
        """The console's computer session: the person's own actions, approvals through the queue."""
        with self._lock:
            if self._session is None:
                from highhx.computer.session import ComputerSession
                from highhx.safety.actions import Actor
                from highhx.safety.audit import AuditLog
                from highhx.safety.gate import ActionGate, ApprovalMode

                gate = ActionGate(
                    self.app.engine,
                    self.prompter,
                    source="web",
                    mode=ApprovalMode.ASK,
                    audit=AuditLog(self.app.db, self.app.redactor) if self.app.db is not None else None,
                )
                self._session = ComputerSession(gate, actor=Actor.USER, tool="web", headless=self.headless)
            return self._session

    def start_task(self, goal: str, surface: str, steps: list[dict[str, Any]] | None) -> WebTask:
        from highhx.actions.catalog import catalog_for
        from highhx.actions.executor import ActionExecutor
        from highhx.agent.loop import AgentLoop, ResolverPlanner, ScriptedPlanner
        from highhx.observability.tasktrace import TraceStore
        from highhx.trajectories import TrajectoryStore
        from highhx.utils.hashing import new_id

        session = self.session()
        task = WebTask(new_id("web"), goal, surface)
        executor = ActionExecutor(
            self.app, session.gate, actor=session.actor, catalog=catalog_for(self.app), computer=lambda: session
        )
        planner = ScriptedPlanner(steps) if steps else ResolverPlanner(self.app, goal, executor=executor)
        task.cancel = self.app.ctx.cancel.child()
        loop = AgentLoop(
            executor,
            planner,
            store=TrajectoryStore.for_app(self.app),
            traces=TraceStore.for_app(self.app),
            agent="web",
            pause=task.pause,
        )

        def run() -> None:
            task.status = "queued"
            with self._run_lock:  # one task at a time: the executor's cancellation is per application
                if task.cancel.cancelled:
                    task.status, task.summary, task.ended = "cancelled", "cancelled before it started", time.time()
                    executor.close()
                    return
                self._run(task, loop, executor, goal, surface)

        task.thread = threading.Thread(target=run, name=f"task-{task.id}", daemon=True)
        self.tasks[task.id] = task
        task.thread.start()
        return task

    def _run(self, task: WebTask, loop: Any, executor: Any, goal: str, surface: str) -> None:
        from highhx.agent.loop import AgentTask

        task.status = "running"
        previous = self.app.ctx.cancel
        try:
            self.app.ctx.cancel = task.cancel
            result = loop.run(AgentTask(goal, surface=surface))
            task.status, task.summary, task.trace_id = str(result.status), result.summary, result.trajectory.trace_id
        except Exception as exc:
            task.status, task.summary = "failed", str(exc)[:300]
        finally:
            self.app.ctx.cancel = previous
            task.ended = time.time()
            executor.close()

    def control(self, task_id: str, op: str) -> WebTask:
        task = self.tasks[task_id]
        if op == "pause":
            task.pause.set()
        elif op == "resume":
            task.pause.clear()
        elif op == "cancel":
            task.pause.clear()
            if task.cancel is not None:
                task.cancel.cancel("cancelled from the web console")
        else:
            raise ValueError(f"unknown control {op!r}")
        return task

    # ------------------------------------------------------------- live view
    def stream(self, source: str) -> FrameBroker:
        broker = self.brokers[source]
        if source not in self.streamers or not self.streamers[source].running:
            if source == "browser":
                streamer: Any = BrowserScreencast(self.session().browser, broker)
            else:
                session = self.session()

                def capture() -> bytes:
                    return bytes(session.driver().screenshot().path.read_bytes())

                streamer = ScreenshotPoller(capture, broker, fps=1.0)
            self.streamers[source] = streamer
            streamer.start()
        return broker

    # ----------------------------------------------------------------- reads
    def snapshot(self) -> dict[str, Any]:
        from highhx.diagnostics.capabilities import capability_report

        return {
            "tasks": [t.to_dict() for t in sorted(self.tasks.values(), key=lambda t: t.started, reverse=True)],
            "approvals": [a.to_dict() for a in self.approvals.pending()],
            "streams": {
                k: {**b.stats(), "error": getattr(self.streamers.get(k), "error", "")} for k, b in self.brokers.items()
            },
            "capabilities": self._capabilities(capability_report),
        }

    def _capabilities(self, report: Any) -> list[dict[str, Any]]:
        """The capability report, refreshed at most once a minute (the page polls every 1.5 s)."""
        now = time.monotonic()
        if self._caps is None or now - self._caps[0] > 60:
            self._caps = (now, [c.to_dict() for c in report(self.app)])
        return self._caps[1]

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        from highhx.trajectories import TrajectoryStore

        out = []
        for t in TrajectoryStore.for_app(self.app).recent(limit=limit):
            out.append(
                {
                    "id": t.id,
                    "task": t.task,
                    "status": t.status,
                    "surface": t.surface,
                    "steps": len(t.steps),
                    "started": t.started,
                    "trace_id": t.trace_id,
                }
            )
        return out

    def task_detail(self, task_id: str) -> dict[str, Any]:
        from highhx.observability.tasktrace import TraceStore
        from highhx.trajectories import TrajectoryStore

        store = TrajectoryStore.for_app(self.app)
        trajectory = store.load(task_id)
        detail: dict[str, Any] = {"trajectory": trajectory.to_dict()}
        with contextlib.suppress(Exception):
            detail["trace"] = TraceStore.for_app(self.app).load(trajectory.trace_id).to_dict()
        return detail

    def events_since(self, index: int, limit: int = 500) -> tuple[int, list[dict[str, Any]]]:
        records = self.recorder.snapshot()
        chosen = records[index : index + limit]
        return index + len(chosen), [r.to_dict() for r in chosen]

    def benchmarks(self) -> list[dict[str, Any]]:
        from highhx.benchmarks.store import BenchmarkStore

        store = BenchmarkStore.for_app(self.app)
        out = []
        for benchmark_id in store.ids()[:20]:
            data = store.load(benchmark_id).to_dict()
            row: dict[str, Any] = {
                k: data[k] for k in ("benchmark_id", "suite", "started", "summary", "skipped") if k in data
            }
            out.append(row)
        return out


class _Handler(BaseHTTPRequestHandler):
    console: WebConsole
    server_version = "HighhX"
    sys_version = ""

    def log_message(self, *args: Any) -> None:  # requests are not logged (URLs carry the token)
        pass

    # ------------------------------------------------------------------ guard
    def _authorized(self, *, state_changing: bool) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.console.port}", f"localhost:{self.console.port}"):
            self._send(421, {"error": "wrong host"})
            return False
        query = parse_qs(urlparse(self.path).query)
        offered = self.headers.get("X-HighhX-Token") or ""
        if not state_changing and not offered:
            offered = (query.get("token") or [""])[0]
        if not offered or not hmac.compare_digest(offered.encode(), self.console.token.encode()):
            self._send(401, {"error": "token required"})
            return False
        if state_changing:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in (
                f"http://127.0.0.1:{self.console.port}",
                f"http://localhost:{self.console.port}",
            ):
                self._send(403, {"error": "cross-origin request refused"})
                return False
        return True

    def _send(self, status: int, data: Any, content_type: str = "application/json") -> None:
        body = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, nonce: str = "") -> None:
        script = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("a JSON object is expected")
        return data

    # ----------------------------------------------------------------- routes
    def do_GET(self) -> None:
        if not self._authorized(state_changing=False):
            return
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        c = self.console
        try:
            if path == "/":
                from highhx.ui.web_page import page

                nonce = secrets.token_urlsafe(12)
                body = page(c.token, nonce).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._security_headers(nonce)
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._send(200, c.snapshot())
            elif path == "/api/history":
                self._send(200, {"tasks": c.history()})
            elif path.startswith("/api/history/"):
                self._send(200, c.task_detail(path.rsplit("/", 1)[1]))
            elif path == "/api/events":
                after = int((query.get("after") or ["0"])[0])
                index, events = c.events_since(after)
                self._send(200, {"next": index, "events": events})
            elif path.startswith("/api/timeline/"):
                from highhx.observability.tasktrace import TraceStore

                rows = (
                    TraceStore.for_app(c.app)
                    .load(path.rsplit("/", 1)[1])
                    .timeline(
                        component=(query.get("component") or [""])[0],
                        search=(query.get("search") or [""])[0],
                        failures=(query.get("failures") or [""])[0] == "1",
                    )
                )
                self._send(200, {"events": rows})
            elif path == "/api/approvals":
                self._send(200, {"approvals": [a.to_dict() for a in c.approvals.all()]})
            elif path == "/api/benchmarks":
                self._send(200, {"benchmarks": c.benchmarks()})
            elif path == "/api/artifacts":
                from highhx.artifacts import ArtifactStore

                items = ArtifactStore.for_app(c.app).list(task_id=(query.get("task") or [""])[0])
                self._send(200, {"artifacts": [a.to_dict() for a in items[-200:]]})
            elif path.startswith("/api/artifacts/"):
                from highhx.artifacts import ArtifactStore

                store = ArtifactStore.for_app(c.app)
                artifact = store.get(path.rsplit("/", 1)[1])
                if not artifact.mime.startswith("image/"):
                    self._send(415, {"error": "only images are shown in the console; export others with the CLI"})
                    return
                self._send(200, store.read(artifact.id), artifact.mime)
            elif path in ("/api/frame", "/api/stream"):
                source = (query.get("source") or ["browser"])[0]
                if source not in c.brokers:
                    self._send(400, {"error": "source is browser or desktop"})
                    return
                broker = c.stream(source)
                if path == "/api/frame":
                    frame = broker.wait_next(int((query.get("after") or ["0"])[0]), timeout=5.0)
                    if frame is None:
                        self._send(204, b"", "text/plain")
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", frame.mime)
                        self.send_header("X-Frame-Seq", str(frame.seq))
                        self.send_header("Content-Length", str(len(frame.data)))
                        self._security_headers()
                        self.end_headers()
                        self.wfile.write(frame.data)
                else:
                    self._mjpeg(broker, float((query.get("fps") or ["5"])[0]))
            else:
                self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except UsageError as exc:
            self._send(404, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _mjpeg(self, broker: FrameBroker, fps: float) -> None:
        """multipart/x-mixed-replace: one part per frame, at most ``fps`` per second (bandwidth)."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self._security_headers()
        self.end_headers()
        interval = 1.0 / max(0.2, min(fps, 15.0))
        seq = 0
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            frame = broker.wait_next(seq, timeout=10.0)
            if frame is None:
                continue
            seq = frame.seq
            part = (
                b"--frame\r\nContent-Type: "
                + frame.mime.encode()
                + b"\r\nContent-Length: "
                + str(len(frame.data)).encode()
                + b"\r\n\r\n"
                + frame.data
                + b"\r\n"
            )
            self.wfile.write(part)
            self.wfile.flush()
            time.sleep(interval)

    def do_POST(self) -> None:
        if not self._authorized(state_changing=True):
            return
        path = urlparse(self.path).path
        c = self.console
        try:
            body = self._body()
            if path == "/api/tasks":
                goal = str(body.get("goal") or "").strip()
                if not goal:
                    raise ValueError("a goal is required")
                surface = str(body.get("surface") or "browser")
                if surface not in ("browser", "desktop", "android", "none", "auto"):
                    raise ValueError("surface is browser, desktop, android, none or auto")
                steps = body.get("steps")
                if steps is not None and not (isinstance(steps, list) and all(isinstance(s, dict) for s in steps)):
                    raise ValueError("steps is a list of step objects")
                self._send(201, c.start_task(goal, surface, steps).to_dict())
            elif path.startswith("/api/tasks/") and path.count("/") == 4:
                _, _, _, task_id, op = path.split("/")
                self._send(200, c.control(task_id, op).to_dict())
            elif path.startswith("/api/approvals/"):
                item = c.approvals.decide(
                    path.rsplit("/", 1)[1],
                    str(body.get("decision") or ""),
                    by="web console",
                    typed=str(body.get("typed") or ""),
                    inputs=body.get("inputs") if isinstance(body.get("inputs"), dict) else None,
                    note=str(body.get("note") or "")[:300],
                )
                self._send(200, item.to_dict())
            else:
                self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
