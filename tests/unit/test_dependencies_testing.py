import json
from pathlib import Path

import pytest

from highhx.core.errors import UsageError
from highhx.dependencies.audit import parse_audit, severity_counts
from highhx.dependencies.detector import adapter_for
from highhx.dependencies.outdated import OutdatedPackage, parse_outdated
from highhx.dependencies.updater import update_command
from highhx.testing.coverage import coverage_command
from highhx.testing.discovery import infer_framework
from highhx.testing.runner import parse_summary, related_python_tests, with_args


def test_outdated_parsers() -> None:
    pip = parse_outdated("pip-json", json.dumps([{"name": "rich", "version": "13.0.0", "latest_version": "14.1.0"}]))
    assert pip[0].update_type == "major"
    npm = parse_outdated(
        "npm-json", json.dumps({"react": {"current": "18.2.0", "wanted": "18.3.1", "latest": "19.0.0"}})
    )
    assert (npm[0].wanted, npm[0].latest) == ("18.3.1", "19.0.0")
    poetry = parse_outdated("poetry-text", "requests 2.31.0 2.32.3 Python HTTP\nclick    8.1.0  8.1.7  CLI\n")
    assert [(p.name, p.latest) for p in poetry] == [("requests", "2.32.3"), ("click", "8.1.7")]
    pub = parse_outdated(
        "pub-json",
        json.dumps(
            {
                "packages": [
                    {
                        "package": "http",
                        "current": {"version": "1.0.0"},
                        "latest": {"version": "1.2.0"},
                        "upgradable": {"version": "1.1.0"},
                    }
                ]
            }
        ),
    )
    assert pub[0].update_type == "minor"
    go = parse_outdated(
        "go-json",
        '{"Path":"a","Main":true}\n{"Path":"github.com/x/y","Version":"v1.0.0","Update":{"Version":"v1.0.1"}}',
    )
    assert go[0].update_type == "patch"
    assert OutdatedPackage("x", None, "1").update_type == "unknown"


def test_audit_parsers() -> None:
    pip = parse_audit(
        "pip-audit",
        json.dumps(
            {
                "dependencies": [
                    {"name": "jinja2", "version": "2.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["3.1.4"]}]}
                ]
            }
        ),
    )
    assert pip[0].id == "PYSEC-1" and pip[0].fix_versions == ["3.1.4"]
    npm = parse_audit(
        "npm-audit",
        json.dumps(
            {
                "vulnerabilities": {
                    "lodash": {
                        "severity": "high",
                        "range": "<4.17.21",
                        "via": [{"url": "GHSA-x", "title": "Prototype pollution"}],
                        "fixAvailable": {"version": "4.17.21"},
                    }
                }
            }
        ),
    )
    assert npm[0].severity == "high" and severity_counts(npm) == {"high": 1}
    yarn = parse_audit(
        "yarn-audit",
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {"advisory": {"module_name": "minimist", "severity": "moderate", "id": 1}},
            }
        ),
    )
    assert yarn[0].severity == "moderate"


def test_adapters_and_update_commands(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}")
    npm = adapter_for("npm", tmp_path)
    assert npm is not None and npm.install == ["npm", "ci"]
    assert update_command(npm, ["react"]) == ["npm", "update", "react"]
    pip = adapter_for("pip", tmp_path)
    assert pip is not None
    with pytest.raises(UsageError):
        update_command(pip, [])
    cargo = adapter_for("cargo", tmp_path)
    assert cargo is not None and update_command(cargo, ["a", "b"]) == ["cargo", "update", "-p", "a", "-p", "b"]


@pytest.mark.parametrize(
    ("framework", "output", "expected"),
    [
        ("pytest", "==== 12 passed, 1 failed, 2 skipped in 0.52s ====", (12, 1, 2, 15)),
        ("unittest", "Ran 5 tests in 0.010s\n\nFAILED (failures=1, skipped=1)", (3, 1, 1, 5)),
        ("jest", "Tests:       1 failed, 11 passed, 12 total", (11, 1, 0, 12)),
        ("vitest", "      Tests  8 passed (8)", (8, 0, 0, 8)),
        ("flutter", "00:02 +4 -1: Some tests failed.", (4, 1, 0, 5)),
        ("maven", "Tests run: 10, Failures: 1, Errors: 0, Skipped: 2", (7, 1, 2, 10)),
        ("cargo", "test result: ok. 3 passed; 0 failed; 1 ignored", (3, 0, 1, 4)),
        ("go", "ok  \tpkg/a\t0.1s\nFAIL\tpkg/b\t0.2s", (1, 1, 0, 2)),
    ],
)
def test_summary_parsing(framework: str, output: str, expected: tuple[int, int, int, int]) -> None:
    s = parse_summary(framework, output)
    assert s.parsed and (s.passed, s.failed, s.skipped, s.total) == expected


def test_related_python_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("")
    (tmp_path / "tests" / "test_other.py").write_text("")
    assert related_python_tests(tmp_path, ["src/pkg/calc.py", "README.md"]) == ["tests/test_calc.py"]
    assert related_python_tests(tmp_path, ["tests/test_other.py"]) == ["tests/test_other.py"]


def test_framework_helpers() -> None:
    assert infer_framework("uv run pytest -q") == "pytest"
    assert infer_framework("npm test") == "npm"
    assert with_args("npm test", ["--watch=false"], "jest") == "npm test -- --watch=false"
    assert coverage_command("pytest", "pytest", {"pytest-cov"}) == "pytest --cov --cov-report=term-missing"
    assert coverage_command("jest", "npm test", set()) == "npm test -- --coverage"
    with pytest.raises(UsageError):
        coverage_command("ctest", "ctest", set())
