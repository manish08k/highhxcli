"""Project directories with spaces and non-ASCII characters must work end to end."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e


def test_full_flow_in_path_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "My Projects" / "café app"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_x.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
    )
    (root / "pyproject.toml").write_text('[project]\nname = "cafe"\nversion = "1.0.0"\n')

    def hx(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "highhx", *args], cwd=root, capture_output=True, text=True, timeout=120, check=False
        )

    assert hx("init").returncode == 0
    config_path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["commands"]["test"] = f'"{sys.executable}" -m unittest discover -s tests -t .'
    config_path.write_text(yaml.safe_dump(config))
    (root / ".highhx" / "workflows" / "paths.yaml").write_text(
        yaml.safe_dump(
            {"name": "paths", "steps": [{"id": "pwd", "run": f'"{sys.executable}" -c "import os; print(os.getcwd())"'}]}
        )
    )
    assert hx("test").returncode == 0
    run = hx("run", "paths", "--json")
    assert run.returncode == 0
    assert json.loads(hx("logs", "--json").stdout)["lines"][-2].endswith("finished: success")
    assert "café app" in hx("logs").stdout
    assert json.loads(hx("status", "--json").stdout)["project"]["name"] == "cafe"
