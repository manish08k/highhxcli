"""Starting and stopping local background services."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel, classify_command
from highhx.config.schema import ServiceConfig
from highhx.core.engine import Engine
from highhx.core.errors import ExecutionError, ValidationError
from highhx.execution.command import CommandSpec
from highhx.execution.environment import build_environment
from highhx.services.health import service_healthy
from highhx.services.ports import is_port_free, port_owner
from highhx.services.registry import ServiceRegistry, ServiceState
from highhx.utils.filesystem import ensure_dir
from highhx.utils.platform import IS_WINDOWS
from highhx.utils.processes import pid_alive, terminate_process_tree
from highhx.utils.time import iso_now


@dataclass
class ServiceStatus:
    name: str
    running: bool
    pid: int | None
    port: int | None
    healthy: bool | None
    health_message: str
    started_at: str | None
    log_file: str | None
    source: str = "highhx"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ServiceManager:
    def __init__(self, engine: Engine, root: Path, registry: ServiceRegistry, logs_dir: Path) -> None:
        self.engine = engine
        self.root = root
        self.registry = registry
        self.logs_dir = logs_dir

    def _running_state(self, name: str) -> ServiceState | None:
        state = self.registry.read_state(name)
        if state is None:
            return None
        if not pid_alive(state.pid):
            self.registry.clear_state(name)
            return None
        return state

    def status(self) -> list[ServiceStatus]:
        rows = []
        for name, service in self.registry.services.items():
            state = self._running_state(name)
            healthy, message = service_healthy(service) if state else (None, "stopped")
            rows.append(
                ServiceStatus(
                    name,
                    state is not None,
                    state.pid if state else None,
                    service.port,
                    healthy,
                    message,
                    state.started_at if state else None,
                    state.log_file if state else None,
                )
            )
        return rows

    def _spawn(self, service: ServiceConfig) -> ServiceState:
        spec = CommandSpec(service.command, cwd=(self.root / service.cwd) if service.cwd else self.root)
        argv = spec.argv()
        log_path = ensure_dir(self.logs_dir) / f"{service.name}.log"
        env = build_environment({**self.engine.ctx.env, **service.env})
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        with log_path.open("ab") as log:
            log.write(f"\n--- {iso_now()} starting: {service.command}\n".encode())
            log.flush()
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(spec.cwd),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    **kwargs,
                )
            except OSError as exc:
                raise ExecutionError(f"Cannot start service '{service.name}': {exc}") from exc
        # The service is deliberately detached: it outlives this process and is tracked by
        # PID (see ServiceRegistry). Mark the handle as settled so garbage collection of
        # the Popen object does not report a "still running" leak.
        proc.returncode = 0
        return ServiceState(service.name, proc.pid, service.command, iso_now(), str(log_path), service.port)

    def _wait_ready(self, service: ServiceConfig, state: ServiceState) -> tuple[bool, str]:
        deadline = time.monotonic() + service.ready_timeout
        time.sleep(0.3)
        while time.monotonic() < deadline:
            if not pid_alive(state.pid):
                return False, "process exited during startup"
            healthy, message = service_healthy(service)
            if healthy is None:
                return True, "started (no health check configured)"
            if healthy:
                return True, message
            if self.engine.ctx.cancel.wait(0.5):
                return False, "cancelled"
        return False, f"not healthy after {service.ready_timeout:g}s"

    def start(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        if not self.registry.services:
            raise ValidationError(
                "No services are configured.",
                hint="Add `services:` to .highhx/config.yaml, or use `highhx docker up` for Compose.",
            )
        order = self.registry.start_order(names)
        results: list[dict[str, Any]] = []
        with self.engine.operation("services", "start", metadata={"services": order}):
            for name in order:
                service = self.registry.get(name)
                existing = self._running_state(name)
                if existing:
                    results.append({"name": name, "status": "already running", "pid": existing.pid})
                    continue
                if service.port and not is_port_free(service.port):
                    owner = port_owner(service.port)
                    raise ValidationError(
                        f"Port {service.port} needed by '{name}' is already in use"
                        + (f" by {owner.process or 'pid'} {owner.pid}" if owner.pid else "")
                        + ".",
                        hint="Stop the other process or change services.<name>.port.",
                    )
                classification = classify_command(service.command)
                self.engine.approve(
                    f"Start service '{name}': {service.command}",
                    max(classification.risk, RiskLevel.NORMAL),
                    details=classification.reasons,
                    policy_action=f"service:start:{name}",
                    command=service.command,
                )
                if self.engine.dry_run:
                    results.append({"name": name, "status": "would start", "command": service.command})
                    continue
                state = self._spawn(service)
                self.registry.write_state(state)
                ready, message = self._wait_ready(service, state)
                if not ready:
                    terminate_process_tree(state.pid)
                    self.registry.clear_state(name)
                    raise ExecutionError(
                        f"Service '{name}' failed to start: {message}", hint=f"See the log: {state.log_file}"
                    )
                results.append(
                    {"name": name, "status": "started", "pid": state.pid, "message": message, "log": state.log_file}
                )
        return results

    def stop(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        order = list(reversed(self.registry.start_order(names))) if self.registry.services else []
        results: list[dict[str, Any]] = []
        with self.engine.operation("services", "stop"):
            for name in order:
                state = self._running_state(name)
                if state is None:
                    results.append({"name": name, "status": "not running"})
                    continue
                self.engine.approve(
                    f"Stop service '{name}' (pid {state.pid})", RiskLevel.NORMAL, policy_action=f"service:stop:{name}"
                )
                if self.engine.dry_run:
                    results.append({"name": name, "status": "would stop", "pid": state.pid})
                    continue
                stopped = terminate_process_tree(state.pid, group=not IS_WINDOWS)
                if stopped:
                    self.registry.clear_state(name)
                results.append({"name": name, "status": "stopped" if stopped else "failed to stop", "pid": state.pid})
        return results

    def restart(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        self.stop(names)
        return self.start(names)

    def log_file(self, name: str) -> Path:
        self.registry.get(name)
        return self.logs_dir / f"{name}.log"
