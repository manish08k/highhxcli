"""Long-running commands: watch reacts to file changes; logs --follow ends with the execution."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.name == "nt", reason="POSIX signals")]


def hx_popen(root: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "highhx", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def wait_until(predicate, timeout: float = 20) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def test_watch_reruns_command_on_change(tmp_path: Path) -> None:
    log = tmp_path / "runs.log"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("1")
    command = f"import pathlib; p = pathlib.Path(r'{log}'); p.write_text(p.read_text() + 'run\\n' if p.exists() else 'run\\n')"
    proc = hx_popen(
        tmp_path,
        "watch",
        "--interval",
        "0.1",
        "--debounce",
        "0.1",
        "--pattern",
        "*.txt",
        "--path",
        "src",
        "--",
        sys.executable,
        "-c",
        command,
    )
    try:
        wait_until(lambda: log.exists() and log.read_text().count("run") == 1)  # initial run
        time.sleep(0.4)
        (tmp_path / "src" / "a.txt").write_text("22")
        wait_until(lambda: log.read_text().count("run") == 2)
        (tmp_path / "src" / "ignored.py").write_text("x")
        time.sleep(0.8)
        assert log.read_text().count("run") == 2
    finally:
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=20)
    assert proc.returncode in (0, 130)


def test_logs_follow_stops_when_execution_finishes(tmp_path: Path) -> None:
    subprocess.run([sys.executable, "-m", "highhx", "init", "-q"], cwd=tmp_path, check=True)
    doc = {"name": "slow", "steps": [{"id": "s", "run": ""}]}
    doc["steps"][0]["run"] = (
        f'"{sys.executable}" -c "import time; [ (print(\'tick\', i, flush=True), time.sleep(0.4)) for i in range(4) ]"'
    )
    (tmp_path / ".highhx" / "workflows" / "slow.yaml").write_text(yaml.safe_dump(doc))
    runner = hx_popen(tmp_path, "run", "slow")
    try:

        def running_id() -> str | None:
            out = subprocess.run(
                [sys.executable, "-m", "highhx", "history", "--json", "--status", "running"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            executions = json.loads(out.stdout or "{}").get("executions") or []
            return executions[0]["id"] if executions else None

        wait_until(lambda: running_id() is not None)
        follower = subprocess.run(
            [sys.executable, "-m", "highhx", "logs", str(running_id()), "--follow"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        runner.communicate(timeout=60)
    assert follower.returncode == 0
    assert sum(1 for line in follower.stdout.splitlines() if " stdout " in line and "tick" in line) == 4
    assert "step s finished: success" in follower.stdout
    assert "finished: success" in follower.stdout


def test_test_watch_reruns_on_change(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_a.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
    )
    subprocess.run([sys.executable, "-m", "highhx", "init", "-q"], cwd=tmp_path, check=True)
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["commands"] = {"test": f'"{sys.executable}" -m unittest discover -s tests -t .'}
    config_path.write_text(yaml.safe_dump(config))
    proc = hx_popen(tmp_path, "test", "--watch")
    try:
        time.sleep(2.5)
        (tmp_path / "tests" / "test_b.py").write_text(
            "import unittest\nclass U(unittest.TestCase):\n    def test_b(self):\n        pass\n"
        )
        time.sleep(3)
    finally:
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=20)
    assert out.count("Tests passed") >= 2
    assert "Ran 2 tests" in out or "2 passed" in out
