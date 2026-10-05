"""External MCP servers mounted into HighhX: their tools become the agent's tools.

    # .highhx/config.yaml                         # or a Claude-style JSON file: HIGHHX_MCP_CONFIG
    mcp:                                          #   {"mcpServers": {"github": {"command": …, "args": […]}}}
      servers:
        github:
          command: npx
          args: [-y, "@modelcontextprotocol/server-github"]
          env: {GITHUB_TOKEN: "${GITHUB_TOKEN}"}  # ${VAR} is read from the environment, never stored

Each server is a subprocess speaking MCP (JSON-RPC over stdio). :class:`McpClient` connects
(``initialize`` → ``tools/list``), calls tools with a timeout, and knows its state: a server that
exits is ``disconnected`` and is restarted for the *next* call — a call it died during has an
unknown outcome and is never repeated automatically. :class:`McpManager` holds a session's
servers and their status for ``/status``, ``highhx mcp`` and the agent. The tools run through the
agent's approval gate and audit (:mod:`highhx.agent.tools.mcp`); nothing here grants anything.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess  # nosec B404 - argv from the person's own configuration, no shell
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.core.errors import IntegrationError

PROTOCOL = "2025-06-18"
CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
_VAR = re.compile(r"\$\{(\w+)\}")
CONNECTED, CONNECTING, DISCONNECTED, FAILED = "connected", "connecting", "disconnected", "failed"


class McpError(IntegrationError):
    """A server could not be reached, answered with an error, or timed out."""

    def __init__(self, message: str, *, unknown_outcome: bool = False, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.unknown_outcome = unknown_outcome


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = CALL_TIMEOUT

    def argv(self) -> list[str]:
        return [self.command, *self.args]

    def environment(self) -> dict[str, str]:
        """The process environment: HighhX's, plus ``env`` with ``${VAR}`` filled from it."""
        extra = {k: _VAR.sub(lambda m: os.environ.get(m.group(1), ""), v) for k, v in self.env.items()}
        return {**os.environ, **extra}


@dataclass
class RemoteTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = True


@dataclass
class CallResult:
    text: str
    images: list[tuple[str, str]] = field(default_factory=list)
    """(media type, base64 data)"""
    structured: Any = None
    error: bool = False


def load_servers(raw_config: dict[str, Any] | None) -> list[ServerConfig]:
    """Servers from the project config's ``mcp.servers`` and the JSON file ``HIGHHX_MCP_CONFIG``."""
    entries: dict[str, Any] = {}
    section = (raw_config or {}).get("mcp")
    if isinstance(section, dict) and isinstance(section.get("servers"), dict):
        entries.update(section["servers"])
    file = os.environ.get("HIGHHX_MCP_CONFIG")
    if file:
        try:
            data = json.loads(Path(file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise McpError(f"Cannot read HIGHHX_MCP_CONFIG ({file}): {exc}") from None
        entries.update((data.get("mcpServers") or data.get("servers") or {}) if isinstance(data, dict) else {})
    servers = []
    for name, spec in entries.items():
        if not isinstance(spec, dict) or not spec.get("command"):
            raise McpError(f"MCP server {name!r} needs a command.")
        command = spec["command"]
        args = [str(a) for a in spec.get("args") or []]
        if isinstance(command, list):
            command, args = str(command[0]), [*(str(c) for c in command[1:]), *args]
        servers.append(
            ServerConfig(
                str(name),
                str(command),
                tuple(args),
                {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                bool(spec.get("enabled", True)),
                float(spec.get("timeout") or CALL_TIMEOUT),
            )
        )
    return servers


class McpClient:
    """One external MCP server over stdio."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.state = DISCONNECTED
        self.detail = ""
        self.tools: list[RemoteTool] = []
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.connected_at: float | None = None
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        """Start the server, negotiate the protocol and list its tools (or FAILED, with why)."""
        self.close()
        self.state, self.detail = CONNECTING, ""
        if shutil.which(self.config.command) is None and not Path(self.config.command).is_file():
            self._fail(f"{self.config.command} is not installed (or not on PATH)")
        try:
            self._process = subprocess.Popen(  # nosec B603 - the person's configured server, no shell
                self.config.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=self.config.environment(),
                bufsize=1,
            )
        except OSError as exc:
            self._fail(f"could not start: {exc}")
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self._process.stdout,), daemon=True).start()
        try:
            info = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "highhx", "version": _version()},
                },
                CONNECT_TIMEOUT,
            )
            self._notify("notifications/initialized")
            self.server_info = dict(info.get("serverInfo") or {})
            self.server_capabilities = dict(info.get("capabilities") or {})
            self.tools = self._list_tools()
        except McpError as exc:
            self._fail(exc.message)
        self.state, self.connected_at = CONNECTED, time.time()
        self.detail = f"{len(self.tools)} tool(s)" + (
            f", {self.server_info.get('name')} {self.server_info.get('version', '')}".rstrip() if self.server_info else ""
        )

    def _fail(self, why: str) -> None:
        self.close()
        self.state, self.detail = FAILED, why
        raise McpError(f"MCP server {self.name}: {why}")

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def health(self) -> str:
        """Check the connection now (``ping``): the state it is really in."""
        if self.state == CONNECTED and not self.alive():
            self.state, self.detail = DISCONNECTED, "the server exited"
        if self.state == CONNECTED:
            try:
                self._request("ping", {}, 10.0)
            except McpError as exc:
                self.state, self.detail = DISCONNECTED, exc.message
        return self.state

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=3)
        finally:
            if process.stdout is not None:
                process.stdout.close()  # the reader has seen the end of the stream
        if self.state == CONNECTED:
            self.state = DISCONNECTED

    # ------------------------------------------------------------ calls
    def call(self, tool: str, arguments: dict[str, Any]) -> CallResult:
        if self.state != CONNECTED or not self.alive():
            self.connect()  # a server that exited is restarted for a new call — never to repeat one
        try:
            result = self._request("tools/call", {"name": tool, "arguments": arguments}, self.config.timeout)
        except McpError:
            if not self.alive():
                self.state, self.detail = DISCONNECTED, "the server exited during a call"
            raise
        text: list[str] = []
        images: list[tuple[str, str]] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text.append(str(item.get("text") or ""))
            elif item.get("type") == "image" and item.get("data"):
                images.append((str(item.get("mimeType") or "image/png"), str(item["data"])))
            elif item.get("type") == "resource":
                resource = item.get("resource") or {}
                text.append(str(resource.get("text") or resource.get("uri") or ""))
        structured = result.get("structuredContent")
        if not text and structured is not None:
            text.append(json.dumps(structured, indent=1, default=str))
        return CallResult("\n".join(text), images, structured, bool(result.get("isError")))

    # ---------------------------------------------------------- resources
    def resources(self) -> list[dict[str, str]]:
        """The server's resources (``resources/list``): uri, name, MIME type. Read-only."""
        if "resources" not in getattr(self, "server_capabilities", {}):
            raise McpError(f"MCP server {self.name} offers no resources.")
        result = self._request("resources/list", {}, self.config.timeout)
        return [
            {"uri": str(r.get("uri") or ""), "name": str(r.get("name") or ""), "mimeType": str(r.get("mimeType") or "")}
            for r in result.get("resources") or []
            if isinstance(r, dict) and r.get("uri")
        ]

    def read_resource(self, uri: str) -> list[dict[str, str]]:
        """One resource's contents (``resources/read``): text parts (binary parts are summarised)."""
        if "resources" not in getattr(self, "server_capabilities", {}):
            raise McpError(f"MCP server {self.name} offers no resources.")
        result = self._request("resources/read", {"uri": uri}, self.config.timeout)
        out = []
        for item in result.get("contents") or []:
            if isinstance(item, dict):
                text = item.get("text")
                out.append({"uri": str(item.get("uri") or uri), "mimeType": str(item.get("mimeType") or ""), "text": str(text) if text is not None else f"<{len(str(item.get('blob') or ''))} bytes of binary data>"})
        return out

    def _list_tools(self) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _page in range(20):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT)
            for item in result.get("tools") or []:
                annotations = item.get("annotations") or {}
                schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {"type": "object"}
                read_only = bool(annotations.get("readOnlyHint"))
                tools.append(
                    RemoteTool(
                        self.name,
                        str(item.get("name") or ""),
                        str(item.get("description") or ""),
                        schema,
                        read_only,
                        bool(annotations.get("destructiveHint", not read_only)),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return [t for t in tools if t.name]

    # ------------------------------------------------------------ JSON-RPC
    def _read(self, stream: Any) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError(f"MCP server {self.name} is not running.")
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise McpError(f"MCP server {self.name} stopped: {exc}") from None

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            self._next += 1
            request_id = self._next
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(
                        f"MCP server {self.name} did not answer {method} within {timeout:.0f}s.",
                        unknown_outcome=method == "tools/call",
                    )
                try:
                    line = self._lines.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    raise McpError(f"MCP server {self.name} exited.", unknown_outcome=method == "tools/call")
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # not a protocol line (a server writing logs to stdout)
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue  # notifications, server requests, late answers
                if "error" in message:
                    error = message["error"] or {}
                    raise McpError(f"MCP server {self.name}: {error.get('message') or 'error'} ({error.get('code')})")
                result = message.get("result")
                return result if isinstance(result, dict) else {}


class McpManager:
    """The MCP servers of one session (or command): connect all, report each one's state."""

    def __init__(self, servers: list[ServerConfig]) -> None:
        self.clients = {s.name: McpClient(s) for s in servers if s.enabled}
        self.disabled = [s.name for s in servers if not s.enabled]

    def connect_all(self) -> list[str]:
        """Connect every server; the problems, one line each (a failing server does not stop others)."""
        problems = []
        for client in self.clients.values():
            try:
                client.connect()
            except McpError as exc:
                problems.append(exc.message)
        return problems

    def tools(self) -> list[RemoteTool]:
        return [t for c in self.clients.values() if c.state == CONNECTED for t in c.tools]

    def status(self) -> list[dict[str, Any]]:
        rows = [
            {"server": c.name, "state": c.health(), "detail": c.detail, "tools": len(c.tools)} for c in self.clients.values()
        ]
        return rows + [{"server": n, "state": "disabled", "detail": "", "tools": 0} for n in self.disabled]

    def close(self) -> None:
        for client in self.clients.values():
            client.close()


def _version() -> str:
    from highhx import __version__

    return __version__
