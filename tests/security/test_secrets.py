"""Security guarantees: secrets never reach output, logs or history."""

import sys
from pathlib import Path

SECRET = "ghp_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa"


def test_secret_values_are_redacted_everywhere(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    cli("env", "set", f"GITHUB_TOKEN={SECRET}", cwd=tmp_path)
    run = cli("exec", sys.executable, "-c", "import os; print('token=' + os.environ['GITHUB_TOKEN'])", cwd=tmp_path)
    assert run.code == 0
    assert SECRET not in run.stdout and "[REDACTED]" in run.stdout
    logs = cli("logs", "--json", cwd=tmp_path)
    assert SECRET not in logs.stdout
    for path in (tmp_path / ".highhx" / "logs").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="replace")
    db = (tmp_path / ".highhx" / "state" / "highhx.db").read_bytes()
    assert SECRET.encode() not in db


def test_security_scan_finds_committed_secret_without_printing_it(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import git

    (git_python_project / "settings.py").write_text(f"TOKEN = '{SECRET}'\n")
    git(git_python_project, "add", "-A")
    git(git_python_project, "commit", "-qm", "chore: settings")
    result = cli("security", "secrets", "--json", cwd=git_python_project)
    assert result.code == 9
    assert SECRET not in result.stdout
    findings = result.json()["findings"]
    assert findings[0]["path"] == "settings.py" and findings[0]["line"] == 1
    assert cli("security", "secrets", "--fail-on", "never", cwd=git_python_project).code == 0


def test_security_report_markdown(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("security", "report", "-o", "report.md", cwd=git_python_project)
    assert result.code == 0
    text = (git_python_project / "report.md").read_text()
    assert "# Security report" in text and "does not mean the project is secure" in text
