from pathlib import Path

import pytest
import yaml

from highhx.config.loader import deep_merge, load_config
from highhx.config.validation import validate_config
from highhx.core.errors import ConfigError, ValidationError
from highhx.environment.dotenv import (
    DotEnvError,
    format_value,
    load_dotenv,
    parse_dotenv,
    set_dotenv_value,
    unset_dotenv_value,
)
from highhx.environment.manager import EnvironmentManager
from highhx.environment.variables import EnvironmentSpec


def write_config(root: Path, data: dict) -> None:
    (root / ".highhx").mkdir(exist_ok=True)
    (root / ".highhx" / "config.yaml").write_text(yaml.safe_dump(data))


def test_valid_config_loads_typed_models(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "version": 1,
            "project": {"name": "x"},
            "commands": {"test": "pytest"},
            "services": {
                "api": {"command": "python -m http.server", "port": 8000, "health": {"url": "http://127.0.0.1:8000/"}}
            },
            "deploy": {
                "default": "prod",
                "targets": {
                    "prod": {"type": "ssh", "host": "h", "command": "./d.sh", "production": True, "timeout": "5m"}
                },
            },
            "tasks": {"a": {"run": "echo a"}, "b": {"run": ["echo b"], "depends_on": ["a"]}},
            "schedules": [{"name": "nightly", "cron": "0 2 * * *", "workflow": "ci"}],
        },
    )
    config = load_config(tmp_path).config
    assert config.commands["test"] == "pytest"
    assert config.services["api"].health.url == "http://127.0.0.1:8000/"  # type: ignore[union-attr]
    assert config.deploy_targets["prod"].timeout == 300 and config.deploy_targets["prod"].production
    assert config.tasks["b"].depends_on == ["a"]


def test_invalid_config_reports_all_problems() -> None:
    errors = validate_config(
        {
            "comands": {},
            "services": {"api": {"port": 99999}},
            "deploy": {"default": "missing", "targets": {"x": {"type": "ftp"}}},
            "tasks": {"a": {"run": "x", "depends_on": ["ghost"]}},
            "schedules": [{"name": "s", "cron": "61 * * * *", "run": "x"}],
            "approvals": {"auto_approve": "sometimes"},
        }
    )
    text = "\n".join(errors)
    assert "comands: unknown field (did you mean 'commands'?)" in text
    assert "services.api.command: is required" in text
    assert "services.api.port: must be <= 65535" in text
    assert "unknown deployment type 'ftp'" in text
    assert "approvals.auto_approve" in text


def test_semantic_config_checks() -> None:
    errors = validate_config(
        {
            "deploy": {"default": "nope", "targets": {"a": {"type": "local", "command": "x"}}},
            "tasks": {"a": {"run": "x", "depends_on": ["ghost"]}},
            "database": {"url": "postgres://u:p@h/db"},
        }
    )
    text = "\n".join(errors)
    assert "target 'nope' is not defined" in text and "unknown task 'ghost'" in text and "contains a password" in text


def test_config_profiles_overlay(tmp_path: Path) -> None:
    write_config(tmp_path, {"commands": {"test": "pytest", "lint": "ruff check ."}})
    (tmp_path / ".highhx" / "profiles").mkdir()
    (tmp_path / ".highhx" / "profiles" / "ci.yaml").write_text("commands:\n  test: pytest -x\n")
    config = load_config(tmp_path, profile="ci").config
    assert config.commands == {"test": "pytest -x", "lint": "ruff check ."}
    with pytest.raises(ConfigError):
        load_config(tmp_path, profile="missing")
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}


def test_yaml_errors_include_location(tmp_path: Path) -> None:
    (tmp_path / ".highhx").mkdir()
    (tmp_path / ".highhx" / "config.yaml").write_text("project:\n  name: [x\n")
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert "line" in info.value.message


def test_dotenv_parsing() -> None:
    text = 'A=1\nexport B="two words"\nC=\'lit ${A}\'\nD=${A}-x # comment\nE="multi\nline"\n# ignored\nF="esc\\"q"\n'
    values = {e.key: e.value for e in parse_dotenv(text)}
    assert values == {"A": "1", "B": "two words", "C": "lit ${A}", "D": "1-x", "E": "multi\nline", "F": 'esc"q'}
    with pytest.raises(DotEnvError):
        parse_dotenv("NOT VALID")


def test_dotenv_editing_preserves_other_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# header\nA=1\nB=2\n")
    assert set_dotenv_value(path, "A", "new value") is True
    assert set_dotenv_value(path, "C", "3") is False
    assert path.read_text() == '# header\nA="new value"\nB=2\nC=3\n'
    assert unset_dotenv_value(path, "B") and load_dotenv(path) == {"A": "new value", "C": "3"}
    assert format_value("a$b") == '"a\\$b"'


def test_environment_manager_profiles_check_and_diff(tmp_path: Path) -> None:
    spec = EnvironmentSpec.from_dict(
        {
            "default_profile": "development",
            "variables": {
                "DATABASE_URL": {"required": True, "secret": True},
                "PORT": {"default": "8000", "pattern": "^[0-9]+$"},
                "MODE": {"choices": ["a", "b"]},
            },
            "profiles": {
                "development": {"files": [".env"]},
                "production": {"files": [".env.production"], "protected": True},
            },
        }
    )
    (tmp_path / ".env").write_text("DATABASE_URL=sqlite:///dev.db\nMODE=c\n")
    (tmp_path / ".env.production").write_text("DATABASE_URL=postgres://prod\nPORT=abc\n")
    manager = EnvironmentManager(tmp_path, spec)
    checks = {c.name: c for c in manager.check("development")}
    assert checks["DATABASE_URL"].status == "ok"
    assert any(c.status == "fail" and "must be one of" in c.message for c in manager.check("development"))
    assert any(c.status == "fail" and "pattern" in c.message for c in manager.check("production"))
    diff = manager.diff("development", "production")
    changed = {c["name"]: c for c in diff.changed}
    assert "postgres" not in str(changed["DATABASE_URL"])
    assert manager.is_protected("production")
    rows = {r["name"]: r for r in manager.describe("development")}
    assert rows["DATABASE_URL"]["value"].startswith("********") and rows["PORT"]["value"] == "8000"


def test_environment_set_validates_and_protects(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    spec = EnvironmentSpec.from_dict({"variables": {"PORT": {"pattern": "^[0-9]+$"}}})
    manager = EnvironmentManager(tmp_path, spec)
    path, existed = manager.set("API_TOKEN", "abc")
    assert path.name == ".env" and not existed
    assert ".env" in (tmp_path / ".gitignore").read_text()
    with pytest.raises(ValidationError):
        manager.set("PORT", "eighty")
    with pytest.raises(ValidationError):
        manager.set("1BAD", "x")
    import os
    import stat

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
