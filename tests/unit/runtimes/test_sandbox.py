"""Sandboxes: workspace copies without secrets, real isolation (Seatbelt on macOS, bubblewrap on
Linux when installed), network policy, limits, timeouts, process cleanup, patches applied
through the executor, and the runtime interface."""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.policy import Risk
from highhx.drivers.base import CapabilityError
from highhx.runtimes import (
    CloudRuntime,
    LocalRuntime,
    RemoteRuntime,
    ResourceLimits,
    Runtime,
    SandboxManager,
    VMRuntime,
    available_backends,
    choose_backend,
)
from highhx.runtimes.sandbox import seatbelt_profile
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI

SEATBELT = available_backends()["seatbelt"].available
BWRAP = available_backends()["bubblewrap"].available
ISOLATED = "seatbelt" if SEATBELT else ("bubblewrap" if BWRAP else None)
needs_isolation = pytest.mark.skipif(ISOLATED is None, reason="no sandbox backend (sandbox-exec or bwrap) here")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('v1')\n")
    (root / ".env").write_text("API_KEY=super-secret\n")
    (root / "deploy.pem").write_text("-----BEGIN KEY-----\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "big.js").write_text("x")
    return root


def test_the_workspace_copy_leaves_secrets_out(project: Path, tmp_path: Path) -> None:
    sandbox = SandboxManager(tmp_path / "sbx").create(project, isolation="workspace")
    ws = sandbox.workspace
    assert (ws / "src" / "app.py").is_file()
    assert not (ws / ".env").exists() and not (ws / "deploy.pem").exists() and not (ws / "node_modules").exists()
    env = sandbox.environment()
    assert env["HOME"].startswith(str(ws)) and env["HIGHHX_SANDBOX"] == sandbox.id
    assert "API_KEY" not in env and "SSH_AUTH_SOCK" not in env
    assert isinstance(sandbox, Runtime) and not any(c.available for c in sandbox.capabilities() if c.name == "desktop")


def test_exec_patch_and_destroy(project: Path, tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sbx")
    sandbox = manager.create(project, isolation="workspace")
    result = sandbox.exec([sys.executable, "-c", "import pathlib; pathlib.Path('src/app.py').write_text(\"print('v2')\\n\"); pathlib.Path('NEW.md').write_text('hi')"])
    assert result.ok, result.stderr
    diff = sandbox.patch()
    assert "-print('v1')" in diff and "+print('v2')" in diff and "NEW.md" in diff and ".highhx-home" not in diff
    assert (project / "src" / "app.py").read_text() == "print('v1')\n"  # the project is untouched
    assert [s.id for s in manager.all()] == [sandbox.id]
    manager.destroy(sandbox.id)
    assert manager.all() == [] and not sandbox.root.exists()


def test_timeouts_kill_the_whole_process_group(project: Path, tmp_path: Path) -> None:
    sandbox = SandboxManager(tmp_path / "sbx").create(project, isolation="workspace", limits=ResourceLimits(timeout=1.0))
    marker = sandbox.workspace / "child-alive"
    (sandbox.workspace / "child.py").write_text(f"import time, pathlib\ntime.sleep(2.5)\npathlib.Path({str(marker)!r}).write_text('x')\n")
    script = "import subprocess, sys, time; subprocess.Popen([sys.executable, 'child.py']); time.sleep(30)"
    result = sandbox.exec([sys.executable, "-c", script])
    assert result.timed_out and not result.ok
    import time

    time.sleep(3)
    assert not marker.exists()  # the background child was killed with its group


def test_file_size_limit(project: Path, tmp_path: Path) -> None:
    sandbox = SandboxManager(tmp_path / "sbx").create(project, isolation="workspace", limits=ResourceLimits(file_mb=1))
    result = sandbox.exec([sys.executable, "-c", "open('big.bin', 'wb').write(b'x' * (3 * 1024 * 1024))"])
    assert not result.ok and (sandbox.workspace / "big.bin").stat().st_size <= 1024 * 1024


@needs_isolation
def test_writes_outside_the_workspace_are_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    sandbox = SandboxManager(tmp_path / "sbx").create(project, isolation=ISOLATED)
    result = sandbox.exec(["/bin/sh", "-c", f"echo inside > ok.txt && echo escaped > {outside}"])
    assert not result.ok and (sandbox.workspace / "ok.txt").is_file() and not outside.exists()
    target = project / "src" / "app.py"
    sandbox.exec(["/bin/sh", "-c", f"echo hacked > {target}"])
    assert target.read_text() == "print('v1')\n"


@needs_isolation
def test_network_is_denied_by_default_and_allowed_on_request(project: Path, tmp_path: Path) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    def accept() -> None:
        for _ in range(2):
            try:
                connection, _address = server.accept()
                connection.close()
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    probe = [sys.executable, "-c", f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3)"]
    try:
        manager = SandboxManager(tmp_path / "sbx")
        denied = manager.create(project, isolation=ISOLATED, network="deny").exec(probe)
        allowed = manager.create(project, isolation=ISOLATED, network="allow").exec(probe)
    finally:
        server.close()
    assert not denied.ok and allowed.ok, allowed.stderr


@needs_isolation
def test_credential_folders_are_unreadable(project: Path, tmp_path: Path) -> None:
    secret = tmp_path / "creds" / "token"
    secret.parent.mkdir()
    secret.write_text("s3cret-value")
    sandbox = SandboxManager(tmp_path / "sbx").create(project, isolation=ISOLATED, deny_read=[secret.parent])
    result = sandbox.exec(["/bin/cat", str(secret)])
    assert not result.ok and "s3cret-value" not in result.stdout
    profile = seatbelt_profile(tmp_path / "ws", network="deny", deny_read=[secret.parent.resolve()])
    assert "(deny network*)" in profile and f'(subpath "{secret.parent.resolve()}")' in profile


def test_backend_choice_is_honest() -> None:
    found = available_backends()
    assert set(found) == {"seatbelt", "bubblewrap", "docker", "workspace"} and found["workspace"].available
    with pytest.raises(Exception, match="Unknown isolation"):
        choose_backend("chroot")
    unavailable = next((n for n in ("seatbelt", "bubblewrap") if not found[n].available), None)
    if unavailable:
        with pytest.raises(CapabilityError):
            choose_backend(unavailable)


def test_other_runtimes(tmp_path: Path) -> None:
    local = LocalRuntime(tmp_path)
    assert isinstance(local, Runtime) and local.exec([sys.executable, "-c", "print(1)"]).stdout.strip() == "1"
    with pytest.raises(CapabilityError):
        local.driver()
    for unavailable in (VMRuntime, CloudRuntime):
        with pytest.raises(CapabilityError, match="not implemented"):
            unavailable()
    remote = RemoteRuntime("ssh://me@build.example:2222", cwd=tmp_path)
    assert remote._ssh()[-1] == "me@build.example" and "BatchMode=yes" in remote._ssh() and "2222" in remote._ssh()
    from highhx.core.errors import UsageError

    with pytest.raises(UsageError):
        RemoteRuntime("http://x", cwd=tmp_path)


# ------------------------------------------------------------ through the executor
def _executor(make_app, root: Path, ui: RecordingUI | None = None) -> tuple[ActionExecutor, RecordingUI]:
    app = make_app(root)
    ui = ui or RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    return ActionExecutor(app, gate, actor=Actor.USER, sleep=lambda _s: None), ui


def test_sandbox_actions_go_through_the_executor(agent_project: Path, make_app) -> None:
    executor, ui = _executor(make_app, agent_project)
    events: list[str] = []
    executor.events.subscribe("*", lambda e: events.append(e.name))
    created = executor.run("sandbox.create", {"isolation": "workspace"})
    assert created.ok and ui.requests  # a sandbox without confinement is asked for
    sandbox_id = created.output["id"]
    ran = executor.run("sandbox.exec", {"id": sandbox_id, "command": "echo changed > NOTES.md"})
    assert ran.ok and ran.output["exit_code"] == 0
    patch = executor.run("sandbox.patch", {"id": sandbox_id})
    assert patch.ok and "NOTES.md" in patch.output["files"]
    ui.action_answers = [False]
    declined = executor.run("sandbox.apply", {"id": sandbox_id})
    assert declined.status == "denied" and not (agent_project / "NOTES.md").exists()
    applied = executor.run("sandbox.apply", {"id": sandbox_id})
    assert applied.ok and (agent_project / "NOTES.md").read_text() == "changed\n" and applied.changed == ["NOTES.md"]
    assert executor.run("sandbox.destroy", {"id": sandbox_id}).ok
    for name in ("sandbox.created", "sandbox.exec", "sandbox.destroyed", "runtime.started", "runtime.stopped"):
        assert name in events


def test_dangerous_commands_stay_dangerous_inside_a_sandbox(agent_project: Path, make_app) -> None:
    executor, _ = _executor(make_app, agent_project)
    planned = executor.plan("sandbox.exec", {"id": "sbx_x", "command": "rm -rf /"})
    assert planned.decision.risk == Risk.CRITICAL
    assert executor.plan("sandbox.create", {"network": "allow"}).decision.risk == Risk.MEDIUM
    (agent_project / ".highhx" / "policies.yaml").write_text("rules:\n  - id: no-sbx\n    effect: deny\n    when: {action: 'sandbox:exec'}\n")
    executor2, _ = _executor(make_app, agent_project)
    assert executor2.run("sandbox.exec", {"id": "sbx_x", "command": "echo hi"}).status == "blocked"


def test_the_kill_switch_destroys_every_sandbox(agent_project: Path, make_app) -> None:
    executor, _ = _executor(make_app, agent_project)
    for _ in range(2):
        executor.run("sandbox.create", {"isolation": "workspace"})
    assert len(executor.run("sandbox.list").output["sandboxes"]) == 2
    assert executor.run("sandbox.destroy", {"all": True}).ok
    assert executor.run("sandbox.list").output["sandboxes"] == []
