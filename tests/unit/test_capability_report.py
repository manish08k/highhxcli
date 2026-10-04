"""`highhx capabilities`: detected by looking (PATH, platform, configuration), never by starting a
browser or contacting a model; missing pieces say what to install; experimental ones say so."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from highhx.diagnostics import capabilities as caps
from tests.unit.agent.conftest import agent_project  # noqa: F401


def by_name(entries: list[caps.CapabilityEntry]) -> dict[str, caps.CapabilityEntry]:
    return {f"{e.area}/{e.name}": e for e in entries}


def test_nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import highhx.computer.browser as browser
    import highhx.drivers.android.adb as adb
    import highhx.runtimes.sandbox as sandbox

    monkeypatch.setattr(browser, "find_browser", lambda: None)
    monkeypatch.setattr(adb, "find_adb", lambda: None)
    monkeypatch.setattr(caps.shutil, "which", lambda name: None)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    for var in ("HIGHHX_PLANNER_BASE_URL", "HIGHHX_SMTP_HOST", "HIGHHX_COMPUTER_TARGET"):
        monkeypatch.delenv(var, raising=False)
    entries = by_name(caps.capability_report())
    assert entries["browser/automation (DOM + accessibility, CDP)"].status == caps.UNAVAILABLE
    assert entries["browser/network evidence"].status == caps.UNAVAILABLE
    android = entries["android/devices over adb"]
    assert android.status == caps.UNAVAILABLE and android.experimental and "adb" in android.detail
    assert entries["perception/OCR"].status == caps.UNAVAILABLE and "tesseract" in entries["perception/OCR"].detail
    assert entries["sandbox/docker"].status == caps.UNAVAILABLE and entries["sandbox/docker"].experimental
    assert entries["sandbox/virtual machines / isolated desktop"].status == caps.NOT_IMPLEMENTED
    assert entries["remote/remote computer (ssh://, keys only)"].status == caps.UNAVAILABLE
    assert entries["models/local planner model (OpenAI-compatible)"].status == caps.NOT_CONFIGURED
    assert entries["workflows/e-mail (email.send)"].status == caps.NOT_CONFIGURED
    assert (
        entries["sandbox/workspace copy (no confinement)"].status == caps.AVAILABLE
    )  # always, and says it confines nothing
    assert "no filesystem or network confinement" in entries["sandbox/workspace copy (no confinement)"].detail


def test_configured_models_are_not_contacted(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the report must not contact anything")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setenv("HIGHHX_PLANNER_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("HIGHHX_PLANNER_MODEL", "qwen2.5:14b")
    entries = by_name(caps.capability_report())
    planner = entries["models/local planner model (OpenAI-compatible)"]
    assert planner.status == caps.AVAILABLE and "not contacted" in planner.detail
    remote = entries["models/remote models"]
    assert (
        remote.status == caps.REQUIRES_PLAN and "--remote-model" in remote.detail and "no provider key" in remote.detail
    )


def test_the_command_reports_json(cli, agent_project: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    result = cli("--json", "capabilities", cwd=agent_project)
    assert result.code == 0, result.stderr
    data = json.loads(result.stdout)
    areas = {e["area"] for e in data["capabilities"]}
    assert {"browser", "desktop", "android", "sandbox", "models", "platform"} <= areas
    assert sum(data["summary"].values()) == len(data["capabilities"])
