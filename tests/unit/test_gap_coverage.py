"""Behaviour of features that previously had no direct tests."""

import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console

from highhx.config.schema import DeployTargetConfig, HighhXConfig, ReleaseConfig
from highhx.core.errors import ApprovalDeniedError, ValidationError
from highhx.deployment.manager import DeploymentManager
from highhx.deployment.state import DeploymentStore
from highhx.git.repository import GitRepository
from highhx.integrations.cloud import register_provider
from highhx.project.detector import detect_project
from highhx.release.manager import ReleaseManager
from highhx.release.publisher import publish_command
from highhx.ui.prompts import ConsolePrompter


def test_console_prompter_reads_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    console = Console(file=io.StringIO())
    prompter = ConsolePrompter(console, interactive=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n\nno\nprod\nwrong\n2\n"))
    assert prompter.confirm("go?") is True
    assert prompter.confirm("go?", default=True) is True
    assert prompter.confirm("go?") is False
    assert prompter.confirm_typed("deploy", "prod") is True
    assert prompter.confirm_typed("deploy", "prod") is False
    assert prompter.choose("pick", ["a", "b"]) == "b"
    silent = ConsolePrompter(console, interactive=False)
    assert (
        silent.confirm("x") is False and silent.confirm_typed("x", "y") is False and silent.ask("x", default="d") == "d"
    )


def test_publish_command_selection(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "lib", "version": "1.0.0"}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    command, problems = publish_command(tmp_path, detect_project(tmp_path), None)
    assert command == "pnpm publish" and problems == []
    assert publish_command(tmp_path, detect_project(tmp_path), "custom publish") == ("custom publish", [])
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]\nname='x'\nversion='1.0.0'\n")
    command, problems = publish_command(py, detect_project(py), None)
    assert command is not None and any("dist/ is empty" in p for p in problems)


def test_publish_is_critical_and_runs_configured_command(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    marker = tmp_path / "published"
    (tmp_path / "package.json").write_text(json.dumps({"name": "lib", "version": "2.0.0"}))
    config = ReleaseConfig(publish_command=f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('ok')\"")
    denied = make_engine(cwd=tmp_path, interactive=False)
    manager = ReleaseManager(
        denied.engine, tmp_path, GitRepository(denied.engine, tmp_path), config, detect_project(tmp_path)
    )
    with pytest.raises(ApprovalDeniedError):
        manager.publish()
    assert not marker.exists()
    kit = make_engine(cwd=tmp_path, interactive=True, answer=True)
    ReleaseManager(
        kit.engine, tmp_path, GitRepository(kit.engine, tmp_path), config, detect_project(tmp_path)
    ).publish()
    assert marker.read_text() == "ok"
    assert "lib" in kit.prompter.asked[-1]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pyproject.toml": "[project]\nname='a'\n", "uv.lock": ""}, "uv build"),
        ({"package.json": '{"name": "a"}', "yarn.lock": ""}, "yarn pack"),
        ({"Cargo.toml": "[package]\nname='a'\nversion='0.1.0'\n"}, "cargo package"),
        ({"Dockerfile": "FROM x\n"}, "docker build -t"),
    ],
)
def test_default_package_commands(tmp_path: Path, files: dict[str, str], expected: str) -> None:
    from highhx.building.packaging import default_package_command

    for name, content in files.items():
        (tmp_path / name).write_text(content)
    command = default_package_command(detect_project(tmp_path))
    assert command is not None and command.startswith(expected)


class RecordingProvider:
    name = "acme"
    calls: list[str] = []

    def deploy(self, target: DeployTargetConfig, context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(f"deploy {context['version']} {target.settings['region']}")
        return {"release": context["version"]}

    def rollback(self, target: DeployTargetConfig, previous: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(f"rollback {previous['release']}")
        return {}

    def status(self, target: DeployTargetConfig, context: dict[str, Any]) -> dict[str, Any]:
        return {"live": True}


def test_plugin_cloud_provider_deploy_and_rollback(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    register_provider("acme", RecordingProvider)
    config = HighhXConfig.from_dict(
        {"deploy": {"targets": {"cloud": {"type": "plugin:acme", "settings": {"region": "eu"}}}}}
    )
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    manager = DeploymentManager(kit.engine, tmp_path, config, DeploymentStore(kit.db))
    assert manager.deploy("cloud", version="1.0.0").ok
    assert manager.deploy("cloud", version="1.1.0").ok
    assert manager.rollback("cloud").ok
    assert RecordingProvider.calls[-3:] == ["deploy 1.0.0 eu", "deploy 1.1.0 eu", "rollback 1.0.0"]
    assert manager.status("cloud")[0]["live"] == {"live": True}
    missing = HighhXConfig.from_dict({"deploy": {"targets": {"x": {"type": "plugin:nobody"}}}})
    with pytest.raises(ValidationError):
        DeploymentManager(kit.engine, tmp_path, missing, DeploymentStore(kit.db)).deploy("x", version="1")


def test_docker_image_deploy_and_rollback(make_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from highhx.core.executor import Executor
    from highhx.execution.process import ProcessOutcome

    calls: list[list[str]] = []

    def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        if argv[:2] == ["docker", "info"]:
            return ProcessOutcome(0, "25\n", "", 0.0)
        return ProcessOutcome(0, "", "", 0.0)

    monkeypatch.setattr("highhx.integrations.docker.client.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("highhx.deployment.strategy.which", lambda name: f"/bin/{name}")
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    config = HighhXConfig.from_dict(
        {"deploy": {"targets": {"d": {"type": "docker", "image": "shop", "service": "web"}}}}
    )
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    kit.engine.executor = Executor(runner=runner)
    manager = DeploymentManager(kit.engine, tmp_path, config, DeploymentStore(kit.db))
    manager.deploy("d", version="1.0.0")
    manager.deploy("d", version="2.0.0")
    calls.clear()
    assert manager.rollback("d").ok
    assert ["docker", "image", "inspect", "shop:1.0.0"] in calls
    assert ["docker", "tag", "shop:1.0.0", "shop:latest"] in calls
    assert ["docker", "compose", "up", "-d", "web"] in calls


def test_fix_benchmark_and_version_commands(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "v"\nversion = "1.4.0"\n')
    cli("init", cwd=tmp_path)
    config_path = tmp_path / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    fixed = tmp_path / "fixed"
    config["commands"] = {
        "fix": f"\"{sys.executable}\" -c \"open(r'{fixed}', 'a').write('fix ')\"",
        "format": f"\"{sys.executable}\" -c \"open(r'{fixed}', 'a').write('format')\"",
    }
    config_path.write_text(yaml.safe_dump(config))
    assert cli("fix", cwd=tmp_path).code == 0 and fixed.read_text() == "fix format"
    bench = cli(
        "benchmark", "-n", "3", "--warmup", "0", "--json", "--", sys.executable, "-c", "pass", cwd=tmp_path
    ).json()
    assert len(bench["durations"]) == 3 and bench["min"] <= bench["median"] <= bench["max"]
    assert cli("version", "minor", "--dry-run", cwd=tmp_path).code == 0
    assert 'version = "1.4.0"' in (tmp_path / "pyproject.toml").read_text()
    assert cli("version", "minor", cwd=tmp_path).code == 0
    assert 'version = "1.5.0"' in (tmp_path / "pyproject.toml").read_text()
    assert cli("version", "2.0.0-rc.1", cwd=tmp_path).code == 0
    assert cli("version", "--json", cwd=tmp_path).json()["version"] == "2.0.0-rc.1"
    assert cli("version", "not-a-version", cwd=tmp_path).code == 2


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_real_npm_install_and_outdated(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Uses the real npm binary on a project without dependencies (no network needed)."""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "offline-app",
                "version": "1.0.0",
                "private": True,
                "scripts": {"test": "node -e \"console.log('ok')\""},
            }
        )
    )
    cli("init", cwd=tmp_path)
    install = cli("deps", "install", "--json", cwd=tmp_path)
    assert install.code == 0, install.stdout
    assert (tmp_path / "package-lock.json").exists()
    outdated = cli("deps", "outdated", "--json", cwd=tmp_path).json()
    assert outdated["reports"][0] == {"manager": "npm", "supported": True, "error": None, "packages": []}
    test = cli("test", "--json", cwd=tmp_path)
    assert test.code == 0 and test.json()["framework"]["name"] == "npm"
