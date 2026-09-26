"""The commands the product brief requires to work end to end."""

import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e._helpers import highhx

pytestmark = pytest.mark.e2e


def test_required_commands(git_python_project: Path) -> None:
    root = git_python_project
    assert highhx("--help", cwd=root).code == 0
    version = highhx("--version", cwd=root)
    assert version.code == 0 and version.stdout.startswith("highhx ")
    assert highhx("init", cwd=root).code == 0
    assert (root / ".highhx" / "config.yaml").exists()
    status = highhx("status", "--json", cwd=root)
    assert status.code == 0 and status.json()["project"]["initialized"] is True
    doctor = highhx("doctor", "--json", cwd=root)
    assert doctor.code in (0, 1) and doctor.json()["checks"]
    assert highhx("workflow", "validate", cwd=root).code == 0
    config_path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["commands"]["test"] = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
    config_path.write_text(yaml.safe_dump(config))
    test = highhx("test", cwd=root)
    assert test.code == 0, test.stdout + test.stderr
    assert "1 passed" in test.stdout


def test_json_output_is_pure(git_python_project: Path) -> None:
    highhx("init", cwd=git_python_project)
    for args in (
        ("status", "--json"),
        ("info", "--json"),
        ("workflow", "list", "--json"),
        ("history", "--json"),
        ("env", "--json"),
    ):
        result = highhx(*args, cwd=git_python_project)
        assert result.code == 0, args
        result.json()
