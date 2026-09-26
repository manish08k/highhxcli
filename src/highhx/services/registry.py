"""Service definitions (from config) and their runtime state files."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from highhx.config.schema import ServiceConfig
from highhx.core.errors import NotFoundError
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.validation import did_you_mean
from highhx.workflows.dependency_graph import DependencyGraph


@dataclass
class ServiceState:
    name: str
    pid: int
    command: str
    started_at: str
    log_file: str
    port: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceRegistry:
    def __init__(self, services: dict[str, ServiceConfig], state_dir: Path) -> None:
        self.services = services
        self.state_dir = state_dir

    def get(self, name: str) -> ServiceConfig:
        if name not in self.services:
            raise NotFoundError(
                f"Unknown service '{name}'{did_you_mean(name, list(self.services))}.",
                hint=f"Services: {', '.join(self.services) or 'none'} (configure them under `services:` in .highhx/config.yaml).",
            )
        return self.services[name]

    def start_order(self, names: list[str] | None = None) -> list[str]:
        """Selected services plus their dependencies, dependencies first."""
        graph = DependencyGraph.from_mapping({n: s.depends_on for n, s in self.services.items()})
        order = graph.topological_order()
        if not names:
            return order
        wanted: set[str] = set()
        for name in names:
            self.get(name)
            wanted.add(name)
            wanted.update(graph.ancestors(name))
        return [n for n in order if n in wanted]

    def state_path(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def read_state(self, name: str) -> ServiceState | None:
        path = self.state_path(name)
        if not path.is_file():
            return None
        try:
            return ServiceState(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def write_state(self, state: ServiceState) -> None:
        atomic_write_text(self.state_path(state.name), json.dumps(state.to_dict(), indent=2))

    def clear_state(self, name: str) -> None:
        self.state_path(name).unlink(missing_ok=True)

    def stale_states(self) -> list[ServiceState]:
        from highhx.utils.processes import pid_alive

        stale = []
        if self.state_dir.is_dir():
            for path in self.state_dir.glob("*.json"):
                state = self.read_state(path.stem)
                if state is not None and not pid_alive(state.pid):
                    stale.append(state)
        return stale
