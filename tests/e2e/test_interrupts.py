"""Real Ctrl+C handling: the CLI exits 130 and leaves no processes behind."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.name == "nt", reason="POSIX signals")]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(path: Path, count: int = 1, timeout: float = 20) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            pids = [int(x) for x in path.read_text().split()]
            if len(pids) >= count:
                return pids
        time.sleep(0.1)
    raise AssertionError(f"{path} not written")


SLEEPER = "import os, time; open(os.environ['PIDFILE'], 'a').write(str(os.getpid()) + ' '); time.sleep(60)"


def spawn(args: list[str], cwd: Path, pidfile: Path) -> subprocess.Popen[str]:
    env = {**os.environ, "PIDFILE": str(pidfile)}
    return subprocess.Popen(
        [sys.executable, "-m", "highhx", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_ctrl_c_during_exec(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    proc = spawn(["exec", "--", sys.executable, "-c", SLEEPER], tmp_path, pidfile)
    (child,) = wait_for(pidfile)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    time.sleep(0.3)
    assert not alive(child)


def test_ctrl_c_during_parallel_workflow(tmp_path: Path) -> None:
    subprocess.run([sys.executable, "-m", "highhx", "init", "-q"], cwd=tmp_path, check=True)
    step = f'"{sys.executable}" -c "{SLEEPER}"'
    doc = {
        "name": "long",
        "settings": {"max_parallel": 3},
        "steps": [{"id": f"s{i}", "run": step} for i in range(3)]
        + [{"id": "after", "run": step, "depends_on": ["s0"]}],
    }
    (tmp_path / ".highhx" / "workflows" / "long.yaml").write_text(yaml.safe_dump(doc))
    pidfile = tmp_path / "pids"
    proc = spawn(["run", "long", "--json"], tmp_path, pidfile)
    children = wait_for(pidfile, 3)
    proc.send_signal(signal.SIGINT)
    _out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 130
    time.sleep(0.3)
    assert not any(alive(pid) for pid in children)
    history = subprocess.run(
        [sys.executable, "-m", "highhx", "history", "--json"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    import json

    latest = json.loads(history.stdout)["executions"][0]
    assert latest["kind"] == "workflow" and latest["status"] == "cancelled"


def test_sigterm_is_graceful(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    proc = spawn(["exec", "--", sys.executable, "-c", SLEEPER], tmp_path, pidfile)
    (child,) = wait_for(pidfile)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    time.sleep(0.3)
    assert not alive(child)
