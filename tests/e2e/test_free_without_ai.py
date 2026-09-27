"""HighhX Free keeps working when AI is unavailable: platform down, no provider keys, no account."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import copy_fixture
from tests.e2e._helpers import highhx

PY = sys.executable

NO_AI = {
    "HIGHHX_API_URL": "http://127.0.0.1:9",  # nothing listens here
    "HIGHHX_TOKEN": "hhx_platform_is_down_0000000000000000",  # highhx:allow-secret (test fixture)
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "HIGHHX_NON_INTERACTIVE": "1",
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = copy_fixture("python_project", tmp_path)
    result = highhx("init", "--yes", cwd=root, env=NO_AI)
    assert result.code == 0, result.stderr
    (root / ".highhx" / "config.yaml").write_text(
        "version: 1\nproject:\n  name: pyapp\ncommands:\n"
        f"  test: {PY} -m pytest -q -p no:cacheprovider\n"
        f"  lint: {PY} -c \"print('lint ok')\"\n"
        f"  typecheck: {PY} -c \"print('types ok')\"\n"
        f"  build: {PY} -c \"import pathlib; pathlib.Path('dist').mkdir(exist_ok=True); pathlib.Path('dist/app.txt').write_text('built')\"\n"
        "services:\n"
        f'  worker:\n    command: {PY} -c "import time; time.sleep(120)"\n'
    )
    for name in ("build", "check", "test", "ci"):
        (root / ".highhx" / "workflows" / f"{name}.yaml").unlink(missing_ok=True)  # use the commands above
    return root


def test_deterministic_commands_work_without_ai(project: Path) -> None:
    for args in (
        ("status", "--json"),
        ("info", "--json"),
        ("check",),
        ("build",),
        ("test",),
        ("do", "run", "the", "tests"),
        ("computer", "status", "--json"),
    ):
        result = highhx(*args, cwd=project, env=NO_AI)
        assert result.code == 0, (args, result.stdout[-2000:], result.stderr[-2000:])
        assert "HighhX platform" not in result.stderr, args
    assert (project / "dist" / "app.txt").read_text() == "built"
    doctor = highhx("doctor", "--json", cwd=project, env=NO_AI)
    assert doctor.code in (0, 1) and "HighhX platform" not in doctor.stderr


def test_services_start_stop_restart_without_ai(project: Path) -> None:
    try:
        assert highhx("start", "worker", cwd=project, env=NO_AI).code == 0
        assert highhx("restart", "worker", cwd=project, env=NO_AI).code == 0
        services = highhx("services", "--json", cwd=project, env=NO_AI)
        assert services.code == 0 and '"running": true' in services.stdout
    finally:
        assert highhx("stop", "worker", cwd=project, env=NO_AI).code == 0


def test_only_the_agent_needs_the_platform(project: Path) -> None:
    agent = highhx("agent", "fix whatever is failing", cwd=project, env=NO_AI)
    assert agent.code == 1 and "Cannot reach the HighhX platform" in agent.stderr
    nondeterministic = highhx("do", "fix", "whatever", "is", "failing", cwd=project, env=NO_AI)
    assert nondeterministic.code == 2 and "highhx agent" in nondeterministic.stderr
