"""Task artifacts: content-addressed, owner-only, checksum-verified, retention, events; registered
by screenshot and download actions; listed by the CLI and the web console."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.artifacts import ArtifactStore
from highhx.core.errors import UsageError
from highhx.core.events import EventBus, trace_context
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


def test_store_add_read_dedupe_delete_and_prune(tmp_path: Path) -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe("artifact.created", seen.append)
    store = ArtifactStore(tmp_path / "artifacts", emit=bus.emit)
    source = tmp_path / "shot.png"
    source.write_bytes(b"\x89PNGdata")
    with trace_context(task_id="task_1", trace_id="tr_1"):
        first = store.add(source, kind="screenshot", action="browser.screenshot")
    second = store.add(source, kind="screenshot")
    assert first.mime == "image/png" and first.size == 8 and first.task_id == "task_1" and first.trace_id == "tr_1"
    assert (
        first.sha256 == second.sha256 and len(list((tmp_path / "artifacts" / "objects").iterdir())) == 1
    )  # stored once
    assert stat.S_IMODE((tmp_path / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "artifacts" / "objects" / first.sha256).stat().st_mode) == 0o600
    assert store.read(first.id) == b"\x89PNGdata" and [a.id for a in store.list(task_id="task_1")] == [first.id]
    assert len(seen) == 2 and seen[0].data["artifact"] == first.id
    (tmp_path / "artifacts" / "objects" / first.sha256).write_bytes(b"tampered")
    with pytest.raises(UsageError, match="checksum"):
        store.read(first.id)
    store.delete(second.id)
    assert (tmp_path / "artifacts" / "objects" / first.sha256).exists()  # the first still refers to it
    assert store.prune(now=time.time() + 31 * 86400) == [first.id]
    assert store.list() == [] and list((tmp_path / "artifacts" / "objects").iterdir()) == []
    with pytest.raises(UsageError, match="kind"):
        store.add(source, kind="bogus")


def test_screenshots_become_artifacts(agent_project: Path, make_app, monkeypatch) -> None:  # noqa: F811
    from highhx.actions.executor import ActionExecutor
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.unit.agent.conftest import RecordingUI
    from tests.unit.agent_loop.fake_web import FakeWebApp

    web = FakeWebApp()
    monkeypatch.setattr(ComputerSession, "browser", property(lambda self: web))
    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    executor = ActionExecutor(app, gate, actor=Actor.USER)
    executor.run("browser.open", {"url": "https://shop.test/"})
    result = executor.run("browser.screenshot", {})
    assert (
        result.ok
        and result.output["artifact"]["kind"] == "screenshot"
        and result.output["artifact"]["action"] == "browser.screenshot"
    )
    assert [a.id for a in ArtifactStore.for_app(app).list(kind="screenshot")] == [result.output["artifact"]["id"]]


def test_the_cli_lists_and_exports_artifacts(cli, agent_project: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from highhx.commands import App

    app = App(cwd=agent_project)
    source = tmp_path / "data.csv"
    source.write_text("a,b\n")
    artifact = ArtifactStore.for_app(app).add(source, kind="generated")
    app.close()
    listed = json.loads(cli("--json", "trajectories", "artifacts", "list", cwd=agent_project).stdout)
    assert [i["id"] for i in listed] == [artifact.id]
    assert cli("trajectories", "artifacts", "export", artifact.id, "out/data.csv", cwd=agent_project).code == 0
    assert (agent_project / "out" / "data.csv").read_text() == "a,b\n"
    assert cli("trajectories", "artifacts", "export", artifact.id, "/tmp/escape.csv", cwd=agent_project).code != 0
