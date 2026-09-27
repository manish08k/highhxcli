"""Workflows as automation graphs: action steps, rollback, resume, cancel, inspection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from highhx.core.result import Status
from highhx.workflows import running
from highhx.workflows.validator import validate_data

PY = sys.executable


def write_workflow(root: Path, name: str, body: str) -> None:
    (root / ".highhx" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".highhx" / "workflows" / f"{name}.yaml").write_text(body)


FLAG_GATE = f"{PY} -c \"import os,sys; sys.exit(0 if os.path.exists('ok.flag') else 1)\""

RELEASE = f"""
name: release-check
on_failure: rollback
steps:
  - id: notes
    action: filesystem.write
    with:
      path: NOTES.md
      content: "notes for ${{{{ inputs.version }}}}\\n"
    rollback: true
  - id: marker
    run: {PY} -c "open('marker.txt','w').write('m')"
    depends_on: notes
    rollback: {PY} -c "import os; os.remove('marker.txt')"
  - id: gate
    run: {FLAG_GATE}
    depends_on: marker
inputs:
  version:
    default: "1.0"
"""


def test_action_steps_rollback_in_reverse_and_resume(agent_project: Path, make_app) -> None:
    write_workflow(agent_project, "release-check", RELEASE)
    app = make_app(agent_project, yes=True, interactive=False)
    failed = app.workflows.run("release-check", inputs={"version": "2.0"})
    assert failed.status == Status.FAILED
    assert [r["step"] for r in failed.rollback] == ["marker", "notes"]  # newest first
    assert all(r["ok"] for r in failed.rollback)
    assert not (agent_project / "NOTES.md").exists() and not (agent_project / "marker.txt").exists()

    (agent_project / "ok.flag").write_text("")
    key, inputs, completed = app.workflows.resume_state(failed.execution_id)
    assert key == "release-check" and inputs == {"version": "2.0"}
    assert completed == {}  # both completed steps were rolled back: they must run again
    resumed = app.workflows.run(key, inputs=inputs, resume=completed)
    assert resumed.ok and (agent_project / "NOTES.md").read_text() == "notes for 2.0\n"


def test_resume_reuses_completed_steps(agent_project: Path, make_app) -> None:
    write_workflow(
        agent_project,
        "two",
        f"""
name: two
steps:
  - id: count
    run: {PY} -c "p='count.txt'; import os; n=int(open(p).read()) if os.path.exists(p) else 0; open(p,'w').write(str(n+1))"
  - id: gate
    run: {FLAG_GATE}
    depends_on: count
""",
    )
    app = make_app(agent_project, interactive=False)
    first = app.workflows.run("two")
    assert first.status == Status.FAILED
    (agent_project / "ok.flag").write_text("")
    key, inputs, completed = app.workflows.resume_state(first.execution_id)
    assert set(completed) == {"count"}
    second = app.workflows.run(key, inputs=inputs, resume=completed)
    assert second.ok and second.steps["count"].message == "already completed (resumed)"
    assert (agent_project / "count.txt").read_text() == "1"  # not run twice


def test_only_unfinished_runs_resume(agent_project: Path, make_app) -> None:
    write_workflow(agent_project, "ok", f"name: ok\nsteps:\n  - id: a\n    run: {PY} -c pass\n")
    app = make_app(agent_project, interactive=False)
    done = app.workflows.run("ok")
    from highhx.core.errors import WorkflowError

    with pytest.raises(WorkflowError, match="only failed"):
        app.workflows.resume_state(done.execution_id)


def test_action_outputs_feed_later_steps(agent_project: Path, make_app) -> None:
    write_workflow(
        agent_project,
        "outputs",
        f"""
name: outputs
steps:
  - id: read
    action: filesystem.read
    with: {{path: pyproject.toml}}
  - id: use
    run: {PY} -c "import sys; sys.exit(0 if sys.argv[1] == 'pyproject.toml' else 1)" ${{{{ steps.read.outputs.path }}}}
    depends_on: read
""",
    )
    app = make_app(agent_project, interactive=False)
    assert app.workflows.run("outputs").ok


def test_denied_action_step_fails_the_workflow(agent_project: Path, make_app) -> None:
    write_workflow(
        agent_project,
        "write",
        "name: write\nsteps:\n  - id: w\n    action: filesystem.write\n    with: {path: a.txt, content: x}\n",
    )
    app = make_app(agent_project, interactive=False)  # nobody can approve a medium-risk change
    result = app.workflows.run("write")
    assert result.status == Status.FAILED and not (agent_project / "a.txt").exists()
    assert "Confirmation required" in result.steps["w"].message


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ("action: nope.nope", "unknown action 'nope.nope'"),
        ("action: filesystem.write\n    with: {path: a.txt}", "content"),
        ("action: git.push\n    rollback: true", "cannot"),
        ("run: echo hi\n    rollback: true", "needs an action step"),
        ("action: git.status\n    rollback: {action: made.up}", "unknown action 'made.up'"),
        ("run: echo hi\n    with: {a: b}", "only valid together with 'uses' or 'action'"),
    ],
)
def test_validation_catches_action_mistakes(step: str, error: str) -> None:
    import yaml

    data = yaml.safe_load(f"name: bad\nsteps:\n  - id: s\n    {step}\n")
    report = validate_data(data, name="bad", check_tools=False)
    assert any(error in e for e in report.errors), report.errors


def test_on_failure_rollback_without_rollbacks_warns() -> None:
    report = validate_data(
        {"name": "w", "on_failure": "rollback", "steps": [{"id": "a", "run": "echo"}]}, name="w", check_tools=False
    )
    assert any("nothing would be undone" in w for w in report.warnings)


def test_running_registry_and_cross_process_cancel(agent_project: Path, tmp_path: Path) -> None:
    write_workflow(
        agent_project, "slow", f'name: slow\nsteps:\n  - id: wait\n    run: {PY} -c "import time; time.sleep(60)"\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "HIGHHX_NON_INTERACTIVE"}
    process = subprocess.Popen(
        [PY, "-m", "highhx", "workflow", "run", "slow"],
        cwd=agent_project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 30
    entry = None
    while time.monotonic() < deadline and entry is None:
        active = [r for r in running.running() if r.workflow == "slow"]
        entry = active[0] if active else None
        time.sleep(0.2)
    assert entry is not None, "the run never registered"
    assert running.find(entry.execution_id[:12]) == entry
    assert running.cancel(entry)
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 130, output
    assert "cancelled" in output
    assert running.find(entry.execution_id) is None  # unregistered


def test_workflow_cli_commands(agent_project: Path, cli) -> None:
    write_workflow(agent_project, "release-check", RELEASE)
    inspected = cli("workflow", "inspect", "release-check", "--json", cwd=agent_project).json()
    assert inspected["on_failure"] == "rollback"
    assert [s["rollback"] for s in inspected["steps"]] == ["compensate", "run", "-"]
    run = cli("workflow", "run", "release-check", "--yes", "--json", cwd=agent_project)
    assert run.code == 1 and run.json()["rollback"]
    runs = cli("workflow", "runs", "--json", cwd=agent_project).json()["runs"]
    assert runs[0]["workflow"] == "release-check" and runs[0]["status"] == "failed"
    record = cli("workflow", "inspect", runs[0]["execution_id"], "--json", cwd=agent_project).json()
    assert {s["step_id"] for s in record["steps"]} >= {"notes", "marker", "gate"}
    (agent_project / "ok.flag").write_text("")
    resumed = cli("workflow", "resume", runs[0]["execution_id"], "--yes", "--json", cwd=agent_project)
    data = json.loads(resumed.stdout)
    assert resumed.code == 0 and data["resumed_from"] == runs[0]["execution_id"]
    cancel = cli("workflow", "cancel", "nothing-running", cwd=agent_project)
    assert cancel.code == 4
