import socket
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e._helpers import highhx

pytestmark = pytest.mark.e2e


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_dev_runs_dev_command(tmp_path: Path) -> None:
    highhx("init", cwd=tmp_path)
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["commands"] = {"dev": f'"{sys.executable}" -c "print(\'dev server ready\')"'}
    config_path.write_text(yaml.safe_dump(config))
    (tmp_path / ".highhx" / "workflows" / "dev.yaml").unlink(missing_ok=True)
    result = highhx("dev", cwd=tmp_path)
    assert result.code == 0 and "dev server ready" in result.stdout


def test_start_status_stop_services(tmp_path: Path) -> None:
    port = free_port()
    highhx("init", cwd=tmp_path)
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["services"] = {
        "web": {
            "command": f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1',
            "port": port,
            "health": {"url": f"http://127.0.0.1:{port}/"},
        }
    }
    config_path.write_text(yaml.safe_dump(config))
    try:
        assert highhx("start", cwd=tmp_path).code == 0
        services = highhx("services", "--json", cwd=tmp_path).json()["services"]
        assert services[0]["running"] and services[0]["healthy"]
        status = highhx("status", "--json", cwd=tmp_path).json()
        assert status["services"][0]["running"]
        ports = highhx("ports", "--json", cwd=tmp_path).json()["ports"]
        assert ports[0]["free"] is False
    finally:
        stop = highhx("stop", cwd=tmp_path)
    assert stop.code == 0
    assert highhx("services", "--json", cwd=tmp_path).json()["services"][0]["running"] is False
