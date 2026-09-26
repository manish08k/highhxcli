"""Every example must validate, and its workflows must run.

Workflows that need toolchains which are not installed (uv, pnpm, flutter, docker …) are
executed with --dry-run, which still validates them, resolves inputs and renders every
command. The basic example needs only Python and runs for real.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted(p for p in (ROOT / "examples").iterdir() if (p / ".highhx").is_dir())


def hx(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, "-m", "highhx", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )


@pytest.fixture
def copy_example(tmp_path: Path):  # type: ignore[no-untyped-def]
    def _copy(example: Path) -> Path:
        target = tmp_path / example.name
        shutil.copytree(example, target)
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
            subprocess.run(["git", "add", "-A"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-qm", "chore: example"], cwd=target, check=True)
        return target

    return _copy


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example_validates_and_dry_runs(example: Path, copy_example) -> None:  # type: ignore[no-untyped-def]
    root = copy_example(example)
    validated = hx(root, "config", "validate")
    assert validated.returncode == 0, validated.stdout + validated.stderr
    workflows = json.loads(hx(root, "workflow", "list", "--json").stdout)["workflows"]
    assert workflows, "every example ships at least one workflow"
    for workflow in workflows:
        text = (root / ".highhx" / "workflows" / Path(workflow["path"]).name).read_text()
        inputs = yaml.safe_load(text).get("inputs") or {}
        args = [a for name, spec in inputs.items() if (spec or {}).get("required") for a in ("--input", f"{name}=x")]
        dry = hx(root, "run", workflow["key"], "--dry-run", *args)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "Dry run" in dry.stdout
    assert hx(root, "doctor", "--json").returncode in (0, 1)


def test_basic_example_runs_for_real(copy_example) -> None:  # type: ignore[no-untyped-def]
    root = copy_example(ROOT / "examples" / "basic")
    check = hx(root, "run", "check", "--json")
    assert check.returncode == 0, check.stdout + check.stderr
    assert {s["id"]: s["status"] for s in json.loads(check.stdout)["steps"]} == {"lint": "success", "test": "success"}
    assert hx(root, "test").returncode == 0
    assert hx(root, "task", "report").returncode == 0
    assert (root / "out" / "report.txt").read_text() == "ok"
    assert "hello from highhx" in hx(root, "script", "hello").stdout


def test_production_example_migrations_run(copy_example, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    root = copy_example(ROOT / "examples" / "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///app.db")
    migrated = hx(root, "db", "migrate", "--yes", "--json")
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    assert json.loads(migrated.stdout)["applied"] == ["001_create_users", "002_add_name"]
    assert hx(root, "policy", "validate").returncode == 0
    targets = json.loads(hx(root, "environments", "--json").stdout)["targets"]
    assert {t["name"] for t in targets} == {"staging", "production"}
