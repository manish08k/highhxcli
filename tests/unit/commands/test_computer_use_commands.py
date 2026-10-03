"""The computer-use CLI: agent loop (plan file, resume), traces, benchmarks (and the old
`highhx benchmark -- COMMAND` / `highhx trace [EXECUTION_ID]` forms), sandboxes, Android without
adb, browser workflows, trajectories, drivers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(
        "steps:\n"
        "  - {action: filesystem.write, parameters: {path: notes.md, content: \"hello\\n\"}, intent: write notes}\n"
        "  - {action: filesystem.read, parameters: {path: notes.md}, intent: read them back}\n"
    )
    return path


def test_agent_loop_with_a_plan_then_its_trace(cli, tmp_path: Path, plan: Path) -> None:
    result = cli("--yes", "--json", "agent", "loop", "write the notes", "--surface", "none", "--plan", str(plan), "--success", '{"file": {"path": "notes.md", "contains": "hello"}}', cwd=tmp_path)
    assert result.code == 0, result.stdout + result.stderr
    data = result.json()
    assert data["status"] == "completed" and data["steps"] == 2 and (tmp_path / "notes.md").read_text() == "hello\n"
    listed = cli("--json", "trace", "list", cwd=tmp_path).json()
    assert listed[0]["trace_id"] == data["trace_id"] and listed[0]["status"] == "completed"
    shown = cli("--json", "trace", data["trace_id"], cwd=tmp_path).json()  # `trace ID` is `trace show ID`
    assert shown["goal"] == "write the notes" and shown["tree"]["children"]
    by_task = cli("--json", "trace", "show", data["task_id"], cwd=tmp_path).json()
    assert by_task["trace_id"] == data["trace_id"]
    exported = tmp_path / "trace.jsonl"
    assert cli("trace", "export", data["trace_id"], "--format", "jsonl", "-o", str(exported), cwd=tmp_path).code == 0
    assert all(json.loads(line)["trace_id"] == data["trace_id"] for line in exported.read_text().splitlines())
    trajectories = cli("--json", "trajectories", "list", cwd=tmp_path).json()
    assert trajectories[0]["id"] == data["task_id"]
    assert cli("trajectories", "show", data["task_id"], cwd=tmp_path).code == 0
    hits = cli("--json", "trajectories", "search", "notes", cwd=tmp_path).json()
    assert hits and hits[0]["id"] == data["task_id"]


def test_without_approval_a_non_interactive_run_is_declined(cli, tmp_path: Path, plan: Path) -> None:
    result = cli("--json", "agent", "loop", "write the notes", "--surface", "none", "--plan", str(plan), cwd=tmp_path)
    assert result.code != 0 and result.json()["status"] == "failed" and not (tmp_path / "notes.md").exists()


def test_resume_routing(cli, tmp_path: Path) -> None:
    missing = cli("agent", "--resume", "task_nothere", cwd=tmp_path)
    assert missing.code != 0 and "No trajectory" in missing.stdout + missing.stderr
    usage = cli("agent", "loop", cwd=tmp_path)
    assert usage.code != 0 and "GOAL" in usage.stdout + usage.stderr


def test_benchmark_commands(cli, tmp_path: Path) -> None:
    suites = cli("--json", "benchmark", "list", cwd=tmp_path).json()
    assert {"browser", "code"} <= {s["suite"] for s in suites}
    run = cli("--json", "benchmark", "run", "code", "-n", "2", cwd=tmp_path)
    assert run.code == 0, run.stdout + run.stderr
    data = run.json()
    assert data["summary"]["runs"] == 4 and data["summary"]["task_success_rate"] == 1.0
    report = cli("--json", "benchmark", "report", cwd=tmp_path).json()
    assert report["benchmark_id"] == data["benchmark_id"]
    second = cli("--json", "benchmark", "run", "code", cwd=tmp_path).json()
    diff = cli("--json", "benchmark", "compare", data["benchmark_id"], second["benchmark_id"], cwd=tmp_path).json()
    assert diff["summary"]["task_success_rate"]["delta"] == 0.0
    timed = cli("--json", "benchmark", "-n", "2", "--warmup", "0", "--", sys.executable, "-c", "pass", cwd=tmp_path)
    assert timed.code == 0 and "mean" in timed.json()  # the original command still works


def test_sandbox_commands(cli, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print(1)\n")
    created = cli("--yes", "--json", "sandbox", "create", "--isolation", "workspace", cwd=tmp_path)
    assert created.code == 0, created.stdout + created.stderr
    sandbox_id = created.json()["output"]["id"]
    ran = cli("--json", "sandbox", "exec", sandbox_id, "--", sys.executable, "-c", "open('new.txt','w').write('x')", cwd=tmp_path)
    assert ran.code == 0 and ran.json()["output"]["exit_code"] == 0
    patch = cli("--json", "sandbox", "patch", sandbox_id, cwd=tmp_path).json()
    assert patch["output"]["files"] == ["new.txt"]
    assert sandbox_id in cli("sandbox", "list", cwd=tmp_path).stdout
    assert cli("sandbox", "destroy", "--all", cwd=tmp_path).code == 0


def test_android_without_adb_is_honest(cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_ADB", "/nonexistent/adb")
    devices = cli("--json", "android", "devices", cwd=tmp_path)
    assert devices.code == 0 and devices.json()["output"]["available"] is False
    tap = cli("--json", "android", "tap", "Save", cwd=tmp_path)
    assert tap.code != 0 and "adb" in tap.json()["error"]


def test_browser_workflows_and_drivers(cli, tmp_path: Path) -> None:
    assert cli("--json", "browser", "workflows", cwd=tmp_path).json() == []
    missing = cli("browser", "replay", "nope", cwd=tmp_path)
    assert missing.code != 0 and "No browser workflow" in missing.stdout + missing.stderr
    drivers = cli("--json", "computer", "drivers", cwd=tmp_path).json()
    assert {d["driver"] for d in drivers["drivers"]} == {"desktop", "browser", "android", "remote", "vm"}
    assert any(b["backend"] == "workspace" and b["available"] for b in drivers["sandbox"])


def test_the_console_needs_a_terminal(cli, tmp_path: Path) -> None:
    assert cli("tui", cwd=tmp_path).code != 0
