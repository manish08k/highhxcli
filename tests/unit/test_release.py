from datetime import date
from pathlib import Path

import pytest

from highhx.git.history import Commit
from highhx.release.changelog import group_commits, insert_section, render_section
from highhx.release.versioning import SemVer, current_version, discover_version_files, suggest_bump, write_version


def commit(subject: str, body: str = "") -> Commit:
    return Commit("x" * 40, "abc1234", "a", "a@x", "2026-01-01", subject, body)


def test_semver_parse_compare_bump() -> None:
    v = SemVer.parse("v1.4.2")
    assert str(v.bump("major")) == "2.0.0" and str(v.bump("minor")) == "1.5.0" and str(v.bump("patch")) == "1.4.3"
    assert str(v.bump("prerelease")) == "1.4.3-rc.1"
    assert str(SemVer.parse("1.4.3-rc.1").bump("prerelease")) == "1.4.3-rc.2"
    assert str(SemVer.parse("1.4.3-rc.1").bump("patch")) == "1.4.3"
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-beta") < SemVer.parse("1.0.0") < SemVer.parse("1.0.1")
    assert SemVer.parse("1.0.0-alpha.2") < SemVer.parse("1.0.0-alpha.10")
    with pytest.raises(ValueError):
        SemVer.parse("1.0")


def test_suggest_bump_from_commits() -> None:
    assert suggest_bump([commit("fix: a")]) == "patch"
    assert suggest_bump([commit("fix: a"), commit("feat: b")]) == "minor"
    assert suggest_bump([commit("feat!: c")]) == "major"
    assert suggest_bump([commit("refactor: x", "BREAKING CHANGE: api")]) == "major"


def test_version_files_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.0.0"\n\n[tool.other]\nversion = "9.9.9"\n'
    )
    (tmp_path / "package.json").write_text(
        '{\n  "name": "x",\n  "version": "1.0.0",\n  "dependencies": {"a": "1.0.0"}\n}\n'
    )
    (tmp_path / "pubspec.yaml").write_text("name: x\nversion: 1.0.0+7\n")
    files = discover_version_files(tmp_path)
    assert {f.path.name for f in files} == {"pyproject.toml", "package.json", "pubspec.yaml"}
    version, problems = current_version(files)
    assert str(version) == "1.0.0" and not problems
    write_version(files, SemVer.parse("1.1.0"))
    assert 'version = "1.1.0"' in (tmp_path / "pyproject.toml").read_text()
    assert 'version = "9.9.9"' in (tmp_path / "pyproject.toml").read_text()
    assert '"a": "1.0.0"' in (tmp_path / "package.json").read_text()
    assert "version: 1.1.0+7" in (tmp_path / "pubspec.yaml").read_text()


def test_disagreeing_version_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (tmp_path / "VERSION").write_text("1.2.0\n")
    version, problems = current_version(discover_version_files(tmp_path))
    assert str(version) == "1.2.0" and "disagree" in problems[0]


def test_changelog_from_real_commits(tmp_path: Path) -> None:
    commits = [
        commit("feat(api): add login"),
        commit("fix: crash"),
        commit("chore: deps"),
        commit("Merge branch x"),
        commit("Update readme"),
    ]
    groups = group_commits(commits)
    assert groups["feat"] == ["**api:** add login (abc1234)"] and groups["fix"] == ["crash (abc1234)"]
    assert "chore" not in str(groups) and "Merge" not in str(groups) and groups["other"] == ["Update readme (abc1234)"]
    section = render_section("1.1.0", commits, on=date(2026, 1, 2))
    assert section.startswith("## [1.1.0] - 2026-01-02")
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2025-01-01\n\n- old\n")
    insert_section(path, section)
    text = path.read_text()
    assert text.index("[Unreleased]") < text.index("[1.1.0]") < text.index("[1.0.0]")
    assert render_section("2.0.0", []).count("No user-facing changes") == 1
