import sys
from pathlib import Path

import yaml


def write_policies(root: Path, rules: list[dict]) -> None:
    path = root / ".highhx" / "policies.yaml"
    data = yaml.safe_load(path.read_text())
    data["rules"] = rules
    path.write_text(yaml.safe_dump(data))


def test_policy_deny_blocks_commands_even_with_yes(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    write_policies(
        tmp_path,
        [
            {
                "id": "no-python-http",
                "when": {"command": "http\\.server"},
                "effect": "deny",
                "message": "no ad-hoc servers",
            }
        ],
    )
    result = cli("exec", "--yes", sys.executable, "-m", "http.server", cwd=tmp_path)
    assert result.code == 7 and "no ad-hoc servers" in result.stderr


def test_non_bypassable_policy_ignores_yes(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    write_policies(
        tmp_path,
        [
            {
                "id": "gate",
                "when": {"command": "echo-gated"},
                "effect": "require_approval",
                "risk": "critical",
                "bypassable": False,
            }
        ],
    )
    result = cli("exec", "--yes", sys.executable, "-c", "print('echo-gated')", cwd=tmp_path)
    assert result.code == 6 and "non-bypassable" in result.stderr


def test_policy_check_flags_forbidden_committed_files(cli, git_python_project: Path) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import git

    cli("init", cwd=git_python_project)
    (git_python_project / "server.pem").write_text("x")
    git(git_python_project, "add", "-f", "server.pem")
    git(git_python_project, "commit", "-qm", "oops")
    result = cli("policy", "check", "--json", cwd=git_python_project)
    assert result.code == 7 and "server.pem" in result.stdout


def test_invalid_policies_file(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    (tmp_path / ".highhx" / "policies.yaml").write_text("rules:\n  - id: x\n    effect: explode\n")
    assert cli("policy", "validate", cwd=tmp_path).code == 3


import pytest  # noqa: E402

from highhx.approvals.risk import RiskLevel, classify_command  # noqa: E402

ADVERSARIAL = {
    "rm -rf ~": RiskLevel.CRITICAL,
    "rm -fr /": RiskLevel.CRITICAL,
    "rm -r -f /": RiskLevel.CRITICAL,
    "sudo rm -rf /*": RiskLevel.CRITICAL,
    'bash -c "rm -rf /"': RiskLevel.CRITICAL,
    "echo $(rm -rf ~)": RiskLevel.CRITICAL,
    "rm -rf $HOME": RiskLevel.CRITICAL,
    ":(){ :|:& };:": RiskLevel.CRITICAL,
    "echo x > /dev/sda": RiskLevel.CRITICAL,
    'mysql -e "DELETE FROM users"': RiskLevel.CRITICAL,
    "find . -delete": RiskLevel.DANGEROUS,
    "find / -exec rm {} +": RiskLevel.DANGEROUS,
    "docker rm -f $(docker ps -aq)": RiskLevel.DANGEROUS,
    "shred -u secret": RiskLevel.DANGEROUS,
    "truncate -s0 app.log": RiskLevel.DANGEROUS,
    "rm -rf build": RiskLevel.DANGEROUS,
    "git status && make deploy": RiskLevel.NORMAL,
    "git fetch; curl x": RiskLevel.NORMAL,
    "rm file.txt": RiskLevel.NORMAL,
    "git status": RiskLevel.SAFE,
}


@pytest.mark.parametrize(("command", "risk"), ADVERSARIAL.items())
def test_adversarial_command_classification(command: str, risk: RiskLevel) -> None:
    assert classify_command(command).risk == risk


@pytest.mark.parametrize("command", ["rm -rf ~", ":(){ :|:& };:", "echo x > /dev/sda", 'bash -c "rm -rf /"'])
def test_catastrophic_commands_are_never_bypassable(command: str) -> None:
    assert not classify_command(command).bypassable


def test_arguments_never_become_shell_syntax(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    marker = tmp_path / "injected"
    payload = f"$(touch {marker}); touch {marker}"
    result = cli("exec", "--json", sys.executable, "-c", "import sys; print(sys.argv[1])", payload, cwd=tmp_path)
    assert result.code == 0
    assert result.json()["stdout"].strip() == payload
    assert not marker.exists()


def test_catastrophic_command_denied_even_with_yes(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = cli("exec", "--yes", 'bash -c "rm -rf /"', cwd=tmp_path)
    assert result.code == 6 and "non-bypassable" in result.stderr
