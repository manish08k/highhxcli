"""``highhx computer engine``: this computer's built-in engine, served as JSON lines on stdin/stdout
— what a remote HighhX reaches through SSH (:mod:`highhx.automation.engine.remote`).

Requests go through this computer's own :class:`~highhx.automation.engine.bridge.AutomationBridge`
(the protocol's validation and the terminal guard apply here too, whatever the caller checked).
A screenshot is taken into this computer's screenshots folder and returned as data, then
removed. Only the protocol's fixed operations exist; there is no way to run anything else.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, TextIO

from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.protocol import PROTOCOL_VERSION, ProtocolError


def serve(bridge: AutomationBridge, reader: TextIO, writer: TextIO) -> int:
    from highhx.computer.driver import HighhXDriver

    shots = HighhXDriver(bridge)
    try:
        for line in reader:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                request_id, op, args = message.get("id"), str(message.get("op") or ""), dict(message.get("args") or {})
            except (ValueError, AttributeError):
                continue
            reply: dict[str, Any] = {"v": PROTOCOL_VERSION, "id": request_id}
            try:
                if op == "screenshot":
                    args["path"] = str(shots.screenshot_path())
                result = bridge.call(op, **args)
                if op == "status":
                    result = {**result, "protocol": PROTOCOL_VERSION}
                if op == "screenshot":
                    path = Path(result["path"])
                    result = {**result, "data": base64.b64encode(path.read_bytes()).decode("ascii")}
                    path.unlink(missing_ok=True)
                reply.update(ok=True, result=result)
            except ProtocolError as exc:
                reply.update(ok=False, error={"code": "invalid_request", "message": str(exc)})
            except EngineError as exc:
                reply.update(ok=False, error={"code": exc.code, "message": exc.message})
            writer.write(json.dumps(reply) + "\n")
            writer.flush()
    finally:
        bridge.close()
    return 0
