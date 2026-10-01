"""Client for the C#/.NET automation engine (``engine/dotnet``): a long-running subprocess that
speaks the bridge protocol as JSON lines on stdin/stdout.

The engine is started once per session and answers a ``status`` handshake, sent at the oldest
supported protocol version; after it HighhX speaks the version the engine reports (from
``MIN_ENGINE_PROTOCOL`` to ``PROTOCOL_VERSION``; newer operations go to the built-in engine).
It is stopped when the session ends. Each request has a deadline; a hung or crashed
engine is killed and reported, never waited on forever.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess  # nosec B404 - fixed argv (the engine binary), no shell
import threading
from pathlib import Path
from typing import IO, Any

from highhx.automation.engine.bridge import EngineError, accessibility_denied
from highhx.automation.engine.protocol import MIN_ENGINE_PROTOCOL, PROTOCOL_VERSION, request
from highhx.execution.cancellation import CancellationToken

REQUEST_TIMEOUT = 30.0
HANDSHAKE_TIMEOUT = 10.0


class SubprocessEngine:
    """An automation engine in a subprocess speaking the protocol as JSON lines: the .NET engine,
    or a remote computer's HighhX engine at the end of an SSH connection."""

    name = "subprocess"
    label = "The automation engine"
    lost = "failed"
    """The error code when the engine (or the link to it) is gone mid-session."""

    def __init__(
        self,
        argv: list[str],
        *,
        cancel: CancellationToken | None = None,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
    ) -> None:
        self.argv = argv
        self.cancel = cancel
        self._next = 0
        self.protocol = MIN_ENGINE_PROTOCOL
        """The version requests are sent at: the oldest for the handshake, then the engine's own."""
        self._lines: queue.Queue[str | None] = queue.Queue()
        try:
            self._process = subprocess.Popen(  # nosec B603 - fixed argv
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise EngineError("not_found", f"{self.label} could not start: {exc}") from None
        assert self._process.stdout is not None
        threading.Thread(target=self._read, args=(self._process.stdout,), daemon=True).start()
        try:
            status = self._send("status", {}, timeout=handshake_timeout)
        except EngineError:
            self.close()
            raise
        self.protocol = int(status.get("protocol") or 0)
        if not MIN_ENGINE_PROTOCOL <= self.protocol <= PROTOCOL_VERSION:
            self.close()
            raise EngineError(
                "failed",
                f"{self.label} speaks protocol {status.get('protocol')}, HighhX needs "
                f"{MIN_ENGINE_PROTOCOL}-{PROTOCOL_VERSION}.",
                hint="Rebuild the engine from this HighhX version's engine/dotnet.",
            )
        self.version = str(status.get("version") or "")

    def _read(self, stream: IO[str]) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, op: str, args: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        if self._process.poll() is not None or self._process.stdin is None:
            raise EngineError(self.lost, f"{self.label} is not running.")
        self._next += 1
        message = request(op, args, self._next, version=self.protocol)
        try:
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise EngineError(self.lost, f"{self.label} stopped: {exc}") from None
        waited = 0.0
        while True:
            if self.cancel is not None and self.cancel.cancelled:
                self.close()
                raise EngineError("failed", f"{op} cancelled.")
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                waited += 0.2
                if waited >= timeout:
                    self.close()
                    raise EngineError("timeout", f"{self.label} did not answer {op} in time.") from None
                continue
            if line is None:
                raise EngineError(self.lost, f"{self.label} exited.")
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # not a protocol line
            if reply.get("id") != self._next:
                continue  # a late answer to a request that timed out
            if reply.get("ok"):
                result = reply.get("result")
                return result if isinstance(result, dict) else {}
            error = reply.get("error") or {}
            code, text = str(error.get("code") or "failed"), str(error.get("message") or "failed")
            if code == "accessibility_denied":
                raise accessibility_denied(text)
            raise EngineError(code, text)

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        timeout = REQUEST_TIMEOUT + (args.get("ms", 0) / 1000 if op == "wait" else 0)
        return self._send(op, args, timeout=timeout)

    def alive(self) -> bool:
        return self._process.poll() is None

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)
        finally:
            if process.stdout is not None and not process.stdout.closed:
                try:
                    process.stdout.close()
                except OSError:
                    pass


class DotnetEngine(SubprocessEngine):
    name = "dotnet"
    label = "The .NET automation engine"

    def __init__(self, binary: Path, *, cancel: CancellationToken | None = None) -> None:
        self.binary = binary
        super().__init__([str(binary), "--stdio"], cancel=cancel)
