"""HighhX's desktop tools for MCP clients: ``highhx computer mcp`` (JSON-RPC 2.0 over stdio).

    MCP client ─ tools/call "click_at" ─► McpServer ─► ActionExecutor.run("computer.click_at")
                                                          classify · approve · audit · verify
                                                          ▼
                                                   HighhXDriver → bridge → engine

Every tool is a ``computer.*`` catalog action run by the same executor and action gate as
``highhx computer …``: the MCP server adds no policy of its own. Permission is fixed when the
server starts, by the person who starts it (as Cua's permission modes are):

- ``--mode read-only``: observation tools only; anything else is not listed and is refused.
- ``--mode ask`` (default): the gate decides; an action that needs a confirmation is refused,
  because nobody can be asked over stdio (the result says so).
- ``--yes``: the person pre-approves each non-critical action a client asks for — the rule of
  their own ``--yes``. Blocked and critical actions are refused regardless.
- ``--allow TOOL`` (repeatable): a bounded manifest — only these tools are listed and callable.

Audit rows carry source ``mcp``. Only the local computer is served; nothing listens on a port.
"""

from __future__ import annotations

import base64
import contextlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from highhx.computer.guidance import COMPUTER_USE_GUIDANCE
from highhx.core.errors import HighhXError
from highhx.safety.actions import ActionKind

if TYPE_CHECKING:
    from highhx.actions.executor import ActionExecutor
    from highhx.actions.spec import ActionSpec

SERVER_NAME = "highhx-computer"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
"""MCP revisions this server speaks, newest first (negotiated in ``initialize``)."""
TOOLS = (
    "observe",
    "screenshot",
    "windows",
    "apps",
    "element_at",
    "verify",
    "launch",
    "focus",
    "click",
    "click_at",
    "move",
    "mouse_button",
    "drag",
    "scroll",
    "type",
    "press",
    "hotkey",
    "menu",
    "window",
    "quit",
    "clipboard_read",
    "clipboard_write",
)
"""Desktop tools, each the ``computer.<name>`` action (the browser has its own, separate path)."""
RUNTIME_TOOLS = {
    "computer_state": "computer.state",
    "browser_open": "browser.open",
    "browser_click": "browser.click",
    "browser_fill": "browser.fill",
    "browser_select": "browser.select",
    "browser_press": "browser.press",
    "browser_scroll": "browser.scroll",
    "browser_extract": "browser.extract",
    "browser_back": "browser.back",
    "android_devices": "android.devices",
    "android_observe": "android.observe",
    "android_tap": "android.tap",
    "android_type": "android.type",
    "android_swipe": "android.swipe",
    "android_key": "android.key",
    "android_launch": "android.launch",
    "android_back": "android.back",
    "android_home": "android.home",
    "sandbox_create": "sandbox.create",
    "sandbox_list": "sandbox.list",
    "sandbox_exec": "sandbox.exec",
    "sandbox_patch": "sandbox.patch",
    "sandbox_destroy": "sandbox.destroy",
    "api_request": "api.request",
}
"""The computer-use runtime's tools (``--toolset runtime``): state, browser, Android, sandboxes, HTTP."""
TOOLSETS = ("desktop", "runtime")
FIXED_INPUTS = {"scroll": {"source": "desktop"}}
"""Inputs the server sets itself (and leaves out of the tool's schema)."""
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


@dataclass
class ToolCall:
    content: list[dict[str, Any]]
    structured: dict[str, Any]
    error: bool

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "structuredContent": self.structured, "isError": self.error}


class McpServer:
    def __init__(
        self,
        executor: ActionExecutor,
        *,
        read_only: bool = False,
        allow: Iterable[str] = (),
        version: str = "",
        toolsets: Iterable[str] = ("desktop",),
    ) -> None:
        from highhx.core.events import new_id

        self.executor = executor
        self.version = version
        self.protocol = PROTOCOL_VERSIONS[0]
        self.session_id = new_id("mcp")
        sets = tuple(toolsets)
        bad = [t for t in sets if t not in TOOLSETS]
        if bad:
            raise HighhXError(f"Unknown toolset(s): {', '.join(bad)}.", hint=f"Toolsets: {', '.join(TOOLSETS)}.")
        offered: dict[str, str] = {}
        if "desktop" in sets:
            offered.update({name: f"computer.{name}" for name in TOOLS})
        if "runtime" in sets:
            offered.update(RUNTIME_TOOLS)
        allowed = set(allow)
        unknown = allowed - set(offered)
        if unknown:
            raise HighhXError(
                f"Unknown tool(s) for --allow: {', '.join(sorted(unknown))}.", hint=f"Tools: {', '.join(offered)}."
            )
        self.known = set(offered)
        self.specs: dict[str, ActionSpec] = {}
        for name, action in offered.items():
            spec = executor.catalog.get(action)
            if spec is None or (allowed and name not in allowed):
                continue
            if read_only and spec.kind != ActionKind.READ:
                continue
            self.specs[name] = spec

    # ---------------------------------------------------------------- tools
    def tool(self, name: str, spec: ActionSpec) -> dict[str, Any]:
        schema = spec.inputs.json_schema()
        for fixed in FIXED_INPUTS.get(name, {}):
            schema["properties"].pop(fixed, None)
        read = spec.kind == ActionKind.READ
        return {
            "name": name,
            "title": spec.name,
            "description": spec.description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": read,
                "destructiveHint": name == "quit",
                "idempotentHint": bool(spec.idempotent),
                "openWorldHint": False,
            },
        }

    def call(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        """One tool call → one ActionRequest through the action protocol and the executor
        (classify · policy · approval · run · verify · audit), with ``tool.*`` events and a
        trace of its own."""
        from highhx.actions import events as ev
        from highhx.actions.protocol import ActionRequest, submit
        from highhx.core.events import new_id, trace_context

        spec = self.specs.get(name)
        if spec is None:
            reason = "not offered by this server" if name in self.known or name in TOOLS else "unknown"
            return _failure(name, "refused", f"Tool {name!r} is {reason} (see tools/list).")
        inputs = {**arguments, **FIXED_INPUTS.get(name, {})}
        request = ActionRequest(spec.name, inputs, intent=f"MCP tool {name}", metadata={"via": "mcp", "tool": name}, trace_id=new_id("tr"))
        bus = self.executor.events
        with trace_context(trace_id=request.trace_id, session_id=self.session_id, source="mcp"):
            bus.emit(ev.TOOL_STARTED, tool=name, action=spec.name)
            try:
                response = submit(self.executor, request)
            except HighhXError as exc:  # unknown action or invalid input: nothing was done
                bus.emit(ev.TOOL_FAILED, tool=name, action=spec.name, status="invalid", error=exc.message)
                return _failure(name, "invalid", exc.message, list(getattr(exc, "details", None) or []))
            result = response.result
            bus.emit(ev.TOOL_COMPLETED if result.ok else ev.TOOL_FAILED, tool=name, action=spec.name, status=result.status, outcome=str(response.outcome))
        data = {"tool": name, **result.to_dict(), "outcome": str(response.outcome), "risk": response.risk.label, "trace_id": request.trace_id}
        text = result.summary or result.status
        if not result.ok:
            text = f"{result.status}: {result.error or result.summary}"
            if result.status == "denied":
                text += " (this server cannot ask anyone; the person who starts it can pre-approve with --yes)"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if result.ok and name == "screenshot":
            content.append(_image(Path(str(result.output.get("path")))))
        return ToolCall(content, data, not result.ok)

    # ------------------------------------------------------------ protocol
    def handle(self, message: Any) -> dict[str, Any] | None:
        """One JSON-RPC message → its response (None for a notification)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if "id" not in message:
            return None  # notifications (initialized, cancelled …) need no answer
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            wanted = str(params.get("protocolVersion") or "")
            self.protocol = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                request_id,
                {
                    "protocolVersion": self.protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "title": "HighhX Computer", "version": self.version},
                    "instructions": COMPUTER_USE_GUIDANCE,
                },
            )
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": [self.tool(n, s) for n, s in self.specs.items()]})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, INVALID_PARAMS, "tools/call needs a name and an arguments object")
            return _result(request_id, self.call(name, arguments).to_dict())
        return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def serve(self, reader: TextIO, writer: TextIO) -> int:
        """Answer newline-delimited JSON-RPC until the client closes stdin. Nothing but protocol
        messages reaches ``writer``: anything else printed while a tool runs goes to stderr."""
        for line in reader:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response: dict[str, Any] | None = _error(None, PARSE_ERROR, "parse error")
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    response = self.handle(message)
            if response is not None:
                writer.write(json.dumps(response, default=str) + "\n")
                writer.flush()
        return 0


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _failure(name: str, status: str, message: str, details: list[str] | None = None) -> ToolCall:
    structured = {"tool": name, "ok": False, "status": status, "error": message, "details": details or []}
    text = f"{status}: {message}" + ("".join(f"\n- {d}" for d in details or []))
    return ToolCall([{"type": "text", "text": text}], structured, True)


def _image(path: Path) -> dict[str, Any]:
    return {"type": "image", "mimeType": "image/png", "data": base64.b64encode(path.read_bytes()).decode("ascii")}
