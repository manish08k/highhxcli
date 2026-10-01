"""A computer at the end of an SSH connection, operated like this one.

    HIGHHX_COMPUTER_TARGET=ssh://me@studio.local        # or computer.target in .highhx/config.yaml
      └─ ssh -T -o BatchMode=yes … me@studio.local highhx computer engine
           remote HighhX: the same bridge (validation, terminal guard) and platform backend

The automation protocol runs over the SSH session's stdin/stdout as JSON lines — the transport of
the .NET engine — so every operation, check and refusal is the same as on this computer, and the
HighhX layers above (executor, approvals, audit, capture grounding, the vision agent, MCP) do not
know the difference. SSH authenticates (keys, ``BatchMode``: never a password prompt HighhX could
answer), encrypts, and detects a dead link (``ServerAlive…``); HighhX listens on no port.

A lost connection is ``connection_lost``: the operation in flight has an unknown outcome and is
never repeated. The computer session starts a new connection for the next action and discards
everything observed through the old one (screenshots, element ids).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from highhx.automation.engine.dotnet_engine import SubprocessEngine
from highhx.core.errors import UsageError
from highhx.execution.cancellation import CancellationToken

SCHEME = "ssh"
CONNECT_TIMEOUT = 30.0
LOST = "Connection lost"
"""How a lost remote connection's errors begin."""


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    user: str = ""
    port: int | None = None
    command: str = "highhx"
    """HighhX on the remote computer (``?highhx=/path/to/highhx`` when it is not on its PATH)."""
    identity: str = ""
    """A private key file (``?identity=~/.ssh/studio``); otherwise SSH's own configuration and agent."""
    known_hosts: str = ""
    """A known-hosts file (``?known_hosts=…``); the host key is always checked, never accepted blindly."""

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def __str__(self) -> str:
        return f"ssh://{self.destination}" + (f":{self.port}" if self.port else "")


def parse_target(target: str) -> RemoteTarget:
    parsed = urlparse(target)
    if parsed.scheme != SCHEME or not parsed.hostname:
        raise UsageError(f"{target!r} is not a computer target: use local or ssh://user@host[:port].")
    query = parse_qs(parsed.query)
    command = query.get("highhx", ["highhx"])[0]
    identity = str(Path(query["identity"][0]).expanduser()) if query.get("identity") else ""
    known = str(Path(query["known_hosts"][0]).expanduser()) if query.get("known_hosts") else ""
    for value in (command, identity, known):
        if any(c in value for c in " ;&|`$<>\n"):
            raise UsageError("Remote paths (highhx, identity, known_hosts) must be plain paths.")
    if not command:
        raise UsageError("The remote highhx path must not be empty.")
    return RemoteTarget(parsed.hostname, parsed.username or "", parsed.port, command, identity, known)


def ssh_argv(target: RemoteTarget) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(CONNECT_TIMEOUT)}",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        *(["-p", str(target.port)] if target.port else []),
        *(["-i", target.identity, "-o", "IdentitiesOnly=yes"] if target.identity else []),
        *(["-o", f"UserKnownHostsFile={target.known_hosts}", "-o", "StrictHostKeyChecking=yes"] if target.known_hosts else []),
        target.destination,
        "--",
        target.command,
        "computer",
        "engine",
    ]


class RemoteEngine(SubprocessEngine):
    name = "remote"
    lost = "connection_lost"

    def __init__(
        self, target: RemoteTarget, *, cancel: CancellationToken | None = None, argv: list[str] | None = None
    ) -> None:
        self.target = target
        self.label = f"The remote computer {target.host}"
        super().__init__(argv or ssh_argv(target), cancel=cancel, handshake_timeout=CONNECT_TIMEOUT)

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op == "screenshot":
            # the remote engine sends the pixels; they land where this computer's protocol allowed
            data = result.pop("data", None)
            if not isinstance(data, str):
                from highhx.automation.engine.bridge import EngineError

                raise EngineError("failed", f"The remote computer {self.target.host} sent no screenshot.")
            Path(args["path"]).write_bytes(base64.b64decode(data))
            result["path"] = args["path"]
        return result
