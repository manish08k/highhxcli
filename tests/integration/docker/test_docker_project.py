from pathlib import Path

import pytest

from tests.conftest import copy_fixture


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    import highhx.integrations.docker.client as client

    monkeypatch.setattr(client, "which", lambda _name: None)


def test_docker_project_init_uses_docker_template(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("docker_project", tmp_path)
    data = cli("init", "--json", cwd=root).json()
    assert data["plan"]["stack"] == "docker"
    targets = cli("environments", "--json", cwd=root).json()["targets"]
    assert targets[0]["type"] == "docker"


def test_docker_commands_without_docker(cli, tmp_path: Path, no_docker) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("docker_project", tmp_path)
    cli("init", cwd=root)
    ps = cli("docker", "--json", cwd=root).json()
    assert ps["installed"] is False and ps["compose_files"] == ["compose.yaml"]
    up = cli("docker", "up", cwd=root)
    assert up.code == 5 and "not installed" in up.stderr
    deploy = cli("deploy", "--yes", cwd=root)
    assert deploy.code != 0


def test_services_lists_compose_files_gracefully(cli, tmp_path: Path, no_docker) -> None:  # type: ignore[no-untyped-def]
    root = copy_fixture("docker_project", tmp_path)
    cli("init", cwd=root)
    assert cli("services", "--json", cwd=root).json() == {"services": [], "compose": []}
