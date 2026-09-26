"""OpenSSH client wrapper (uses the system ``ssh``/``scp``; keys and agents work as usual)."""

from __future__ import annotations

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.utils.processes import which


class SSHClient:
    def __init__(
        self,
        engine: Engine,
        host: str,
        *,
        port: int | None = None,
        user: str | None = None,
        identity_file: str | None = None,
        batch: bool = True,
        connect_timeout: int = 15,
    ) -> None:
        self.engine = engine
        self.host = host
        self.port = port
        self.user = user
        self.identity_file = identity_file
        self.batch = batch
        self.connect_timeout = connect_timeout

    def require(self) -> None:
        if which("ssh") is None:
            raise ToolNotFoundError("ssh", purpose="deploy over SSH", hint="Install an OpenSSH client.")

    @property
    def destination(self) -> str:
        if self.user and "@" not in self.host:
            return f"{self.user}@{self.host}"
        return self.host

    def ssh_args(self) -> list[str]:
        args = ["ssh", "-o", f"ConnectTimeout={self.connect_timeout}"]
        if self.batch:
            args += ["-o", "BatchMode=yes"]
        if self.port:
            args += ["-p", str(self.port)]
        if self.identity_file:
            args += ["-i", self.identity_file]
        return args

    def argv(self, remote_command: str) -> list[str]:
        return [*self.ssh_args(), self.destination, remote_command]

    def run(
        self,
        remote_command: str,
        *,
        risk: RiskLevel = RiskLevel.DANGEROUS,
        timeout: float | None = None,
        approved: bool = False,
        production: bool = False,
        name: str = "ssh",
    ) -> CommandResult:
        self.require()
        return self.engine.run(
            CommandSpec(self.argv(remote_command), timeout=timeout, name=name),
            action=f"Run on {self.destination}: {remote_command}",
            risk=risk,
            approved=approved,
            production=production,
        )

    def check_connection(self) -> CommandResult:
        self.require()
        return self.engine.capture(CommandSpec(self.argv("true"), timeout=self.connect_timeout + 5))
