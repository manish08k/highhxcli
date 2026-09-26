import json
import socket
from pathlib import Path

import pytest

from highhx.config.schema import HighhXConfig
from highhx.core.errors import IntegrationError, ValidationError
from highhx.core.executor import Executor
from highhx.execution.process import ProcessOutcome
from highhx.integrations.docker.client import DockerClient, parse_compose_ps
from highhx.services.manager import ServiceManager
from highhx.services.ports import is_port_free, is_port_open
from highhx.services.registry import ServiceRegistry
from tests.conftest import PY


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def services(tmp_path: Path, make_engine, config: dict):  # type: ignore[no-untyped-def]
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    cfg = HighhXConfig.from_dict({"services": config})
    return ServiceManager(kit.engine, tmp_path, ServiceRegistry(cfg.services, tmp_path / "state"), tmp_path / "logs")


def test_start_status_stop_real_service(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    port = free_port()
    manager = services(
        tmp_path,
        make_engine,
        {
            "db": {"command": f'"{PY}" -c "import time; time.sleep(60)"'},
            "web": {
                "command": f'"{PY}" -m http.server {port} --bind 127.0.0.1',
                "port": port,
                "depends_on": ["db"],
                "health": {"url": f"http://127.0.0.1:{port}/"},
                "ready_timeout": 20,
            },
        },
    )
    try:
        started = manager.start(["web"])
        assert [r["name"] for r in started] == ["db", "web"]
        status = {s.name: s for s in manager.status()}
        assert status["web"].running and status["web"].healthy and is_port_open(port)
        assert manager.start(["web"])[1]["status"] == "already running"
    finally:
        stopped = manager.stop()
    assert {r["status"] for r in stopped} == {"stopped"}
    assert is_port_free(port)


def test_port_conflict_is_reported(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        manager = services(tmp_path, make_engine, {"web": {"command": "x", "port": port}})
        with pytest.raises(ValidationError) as info:
            manager.start()
        assert str(port) in info.value.message


def test_service_crash_during_startup(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    manager = services(
        tmp_path,
        make_engine,
        {"bad": {"command": f'"{PY}" -c "raise SystemExit(1)"', "port": free_port(), "ready_timeout": 5}},
    )
    with pytest.raises(Exception) as info:
        manager.start()
    assert "failed to start" in str(info.value)


def test_parse_compose_ps_formats() -> None:
    lines = '{"Service":"web","State":"running","Status":"Up 2m","Publishers":[{"URL":"0.0.0.0","PublishedPort":8000,"TargetPort":80,"Protocol":"tcp"}]}\n{"Service":"db","State":"exited","Health":""}'
    parsed = parse_compose_ps(lines)
    assert parsed[0].running and parsed[0].ports == "0.0.0.0:8000->80/tcp" and not parsed[1].running
    assert parse_compose_ps(json.dumps([{"Service": "x", "State": "running"}]))[0].name == "x"


class FakeDocker:
    """Records docker invocations and returns scripted results."""

    def __init__(self, daemon: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.daemon = daemon

    def __call__(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "info"]:
            return ProcessOutcome(0 if self.daemon else 1, "24.0.0\n" if self.daemon else "", "", 0.0)
        if argv[:3] == ["docker", "compose", "version"]:
            return ProcessOutcome(0, "v2", "", 0.0)
        if "ps" in argv:
            return ProcessOutcome(0, '{"Service":"web","State":"running"}\n', "", 0.0)
        return ProcessOutcome(0, "", "", 0.0)


def docker_client(tmp_path: Path, make_engine, monkeypatch, fake: FakeDocker, **engine_kwargs):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("highhx.integrations.docker.client.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    kit = make_engine(cwd=tmp_path, **engine_kwargs)
    kit.engine.executor = Executor(runner=fake)
    return DockerClient(kit.engine, tmp_path)


def test_docker_compose_commands(tmp_path: Path, make_engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeDocker()
    client = docker_client(tmp_path, make_engine, monkeypatch, fake, yes=True, interactive=False)
    client.compose_up(["web"], build=True)
    assert ["docker", "compose", "up", "-d", "--build", "web"] in fake.calls
    assert client.compose_ps()[0].name == "web"
    client.compose_down()
    assert ["docker", "compose", "down"] in fake.calls


def test_docker_down_volumes_is_critical(tmp_path: Path, make_engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from highhx.core.errors import ApprovalDeniedError

    fake = FakeDocker()
    client = docker_client(tmp_path, make_engine, monkeypatch, fake, interactive=False)
    with pytest.raises(ApprovalDeniedError):
        client.compose_down(volumes=True)
    assert not any("down" in call for call in fake.calls)


def test_docker_not_running(tmp_path: Path, make_engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = docker_client(tmp_path, make_engine, monkeypatch, FakeDocker(daemon=False), yes=True)
    with pytest.raises(IntegrationError) as info:
        client.compose_up()
    assert info.value.message == "Docker is not running."
