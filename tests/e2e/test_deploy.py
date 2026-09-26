import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import git
from tests.e2e._helpers import highhx

pytestmark = pytest.mark.e2e


def configure_target(root: Path) -> Path:
    marker = root / "live-version.txt"
    config_path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["deploy"] = {
        "default": "local",
        "targets": {
            "local": {
                "type": "local",
                "command": f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('{{{{ version }}}}')\"",
                "rollback_command": f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('{{{{ previous_version }}}}')\"",
                "health_check": {
                    "command": f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(r\'{marker}\').exists() else 1)"',
                    "retries": 2,
                    "interval": 0,
                },
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config))
    (root / ".gitignore").write_text((root / ".gitignore").read_text() + "live-version.txt\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "chore: configure deploy")
    return marker


def test_deploy_requires_approval_and_clean_tree(git_python_project: Path) -> None:
    highhx("init", cwd=git_python_project)
    marker = configure_target(git_python_project)
    denied = highhx("deploy", "--version", "1.0.0", cwd=git_python_project)
    assert denied.code == 6 and not marker.exists()
    (git_python_project / "dirty.txt").write_text("x")
    dirty = highhx("deploy", "--version", "1.0.0", "--yes", cwd=git_python_project)
    assert dirty.code == 8 and "uncommitted" in dirty.stderr
    (git_python_project / "dirty.txt").unlink()
    ok = highhx("deploy", "--version", "1.0.0", "--yes", "--json", cwd=git_python_project)
    assert ok.code == 0, ok.stderr
    assert ok.json()["deployment"]["status"] == "succeeded" and marker.read_text() == "1.0.0"
    status = highhx("deploy", "status", "--json", cwd=git_python_project).json()
    assert status["targets"][0]["latest"]["version"] == "1.0.0"
