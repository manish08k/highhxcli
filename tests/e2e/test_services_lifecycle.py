"""Repeated start/stop of real background services without leaks or false port conflicts."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.name == "nt", reason="POSIX process checks")]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def hx(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "highhx", *args], cwd=root, capture_output=True, text=True, timeout=120, check=False
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_repeated_start_stop_and_shell_wrapped_service(tmp_path: Path) -> None:
    port = free_port()
    pidfile = tmp_path / "grandchild.pid"
    hx(tmp_path, "init", "-q")
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    # Shell syntax (&&) forces `sh -c`, so the real server is a grandchild of HighhX.
    worker = f"import os, http.server; open(r'{pidfile}', 'w').write(str(os.getpid())); http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port={port}, bind='127.0.0.1')"
    config["services"] = {
        "web": {
            "command": f'cd . && "{sys.executable}" -c "{worker}"',
            "port": port,
            "health": {"url": f"http://127.0.0.1:{port}/"},
            "ready_timeout": 20,
        }
    }
    config_path.write_text(yaml.safe_dump(config))
    for _ in range(3):
        started = hx(tmp_path, "start", "--json")
        assert started.returncode == 0, started.stderr
        # Generate a few connections so the port has TIME_WAIT sockets on the next start.
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall(b"GET / HTTP/1.0\r\n\r\n")
                conn.recv(100)
        grandchild = int(pidfile.read_text())
        assert alive(grandchild)
        stopped = hx(tmp_path, "stop", "--json")
        assert json.loads(stopped.stdout)["services"][0]["status"] == "stopped"
        time.sleep(0.3)
        assert not alive(grandchild), "service grandchild survived stop"
    status = json.loads(hx(tmp_path, "services", "--json").stdout)
    assert status["services"][0]["running"] is False
    assert not list((tmp_path / ".highhx" / "state" / "services").glob("*.json"))


def test_stale_pid_file_is_cleaned(tmp_path: Path) -> None:
    hx(tmp_path, "init", "-q")
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["services"] = {"w": {"command": f'"{sys.executable}" -c "import time; time.sleep(60)"'}}
    config_path.write_text(yaml.safe_dump(config))
    state = tmp_path / ".highhx" / "state" / "services"
    state.mkdir(parents=True)
    (state / "w.json").write_text(
        json.dumps({"name": "w", "pid": 999999, "command": "x", "started_at": "", "log_file": "", "port": None})
    )
    diagnosis = json.loads(hx(tmp_path, "diagnose", "--json").stdout)
    assert any(p["id"] == "stale-services" for p in diagnosis["problems"])
    assert hx(tmp_path, "repair", "--yes").returncode == 0
    assert not (state / "w.json").exists()
