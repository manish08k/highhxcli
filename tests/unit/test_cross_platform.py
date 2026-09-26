from pathlib import Path

import pytest

from highhx.utils import paths, platform
from highhx.utils.filesystem import atomic_write_text, ensure_gitignore_entries, iter_files, matches_any
from highhx.utils.time import format_duration, parse_duration
from highhx.utils.validation import Int, List, Obj, OneOf, Prop, Str


def test_user_dirs_per_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("HIGHHX_DATA_DIR", "HIGHHX_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HIGHHX_HOME_OVERRIDE", str(tmp_path))
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(paths, "IS_MACOS", False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.user_config_dir() == tmp_path / "Roaming" / "highhx"
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    monkeypatch.setattr(paths, "IS_MACOS", True)
    assert paths.user_data_dir() == tmp_path / "Library" / "Application Support" / "highhx"
    monkeypatch.setattr(paths, "IS_MACOS", False)
    assert paths.user_data_dir() == tmp_path / ".local" / "share" / "highhx"


def test_executable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    assert platform.executable_name("gradlew") == "gradlew.bat"
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    assert platform.executable_name("gradlew") == "gradlew"


def test_filesystem_helpers(tmp_path: Path) -> None:
    atomic_write_text(tmp_path / "a" / "b.txt", "hello")
    assert (tmp_path / "a" / "b.txt").read_text() == "hello"
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("")
    assert [p.name for p in iter_files(tmp_path)] == ["b.txt"]
    assert matches_any("src/app.min.js", ["*.min.js"]) and matches_any("dist/x", ["dist"])
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules")
    assert ensure_gitignore_entries(gitignore, ["node_modules", ".env"]) == [".env"]
    assert gitignore.read_text().endswith(".env\n")


def test_durations() -> None:
    assert parse_duration("1.5m") == 90 and parse_duration(3) == 3 and parse_duration("250ms") == 0.25
    with pytest.raises(ValueError):
        parse_duration("soon")
    assert format_duration(0.25) == "250ms" and format_duration(125) == "2m 05s" and format_duration(3700) == "1h 01m"


def test_schema_validator_messages() -> None:
    schema = Obj(
        {
            "name": Prop(Str(), required=True),
            "port": Prop(Int(minimum=1)),
            "tags": Prop(List(Str())),
            "mode": Prop(OneOf([Str(choices=["a"]), Int()])),
        }
    )
    errors = schema.validate({"port": 0, "tags": ["x", 1], "nmae": "x", "mode": "b"}, "")
    text = "\n".join(errors)
    assert "name: is required" in text and "port: must be >= 1" in text and "tags[1]: expected a string" in text
    assert "nmae: unknown field (did you mean 'name'?)" in text and "mode: 'b' is not one of: a" in text
    assert schema.json_schema()["required"] == ["name"]


def test_windows_command_parsing_in_validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from highhx.execution import command as command_module
    from highhx.workflows.validator import validate_data

    monkeypatch.setattr(command_module, "IS_WINDOWS", True)
    doc = {"name": "w", "steps": [{"id": "a", "run": r'C:\tools\app.exe "C:\my dir\file.txt"'}]}
    assert validate_data(doc, name="w", check_tools=False).ok


def test_test_args_are_quoted_for_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.execution import command as command_module
    from highhx.testing.runner import with_args

    assert with_args("pytest", ["-k", "a and b"], "pytest") == "pytest -k 'a and b'"
    monkeypatch.setattr(command_module, "IS_WINDOWS", True)
    assert with_args("pytest", ["-k", "a and b"], "pytest") == 'pytest -k "a and b"'


def test_output_decoding_falls_back_to_locale() -> None:
    from highhx.execution.process import _decode

    assert _decode("héllo".encode()) == "héllo"
    assert _decode(b"caf\xe9") in ("café", "caf\ufffd")
