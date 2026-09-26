"""Contract tests for integrations whose real services are not available in CI.

Each external tool (psql, pg_dump, kubectl, terraform, docker) is replaced by a small
executable on PATH that records the argv and environment it receives. This exercises
HighhX's real subprocess path — PATH resolution, argument construction, environment
passing, exit-code handling — without claiming the real service was contacted.
"""

import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest
import yaml

PG_ASSIGNMENT = (
    "DATABASE_URL=postgresql://app:pg-Secret-99@db.internal:5433/shop?sslmode=require"  # highhx:allow-secret
)

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.name == "nt", reason="shebang-based fake tools")]

FAKE = """#!{python}
import json, os, sys
log = os.environ["FAKE_TOOL_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"tool": {name!r}, "argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in ("PGPASSWORD", "MYSQL_PWD")}}}}) + "\\n")
responses = json.loads(os.environ.get("FAKE_TOOL_RESPONSES", "{{}}"))
key = {name!r} + " " + " ".join(a for a in sys.argv[1:] if not a.startswith("-"))
for pattern, (code, out) in responses.items():
    if all(token in key.split() for token in pattern.split()):
        sys.stdout.write(out)
        sys.exit(code)
sys.exit(0)
"""


@pytest.fixture
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"

    def install(*names: str, responses: dict[str, tuple[int, str]] | None = None) -> None:
        for name in names:
            path = bindir / name
            path.write_text(FAKE.format(python=sys.executable, name=name))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("FAKE_TOOL_RESPONSES", json.dumps(responses or {}))

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TOOL_LOG", str(log))

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    install.calls = calls  # type: ignore[attr-defined]
    return install


def write_config(root: Path, update: dict) -> None:
    path = root / ".highhx" / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config.update(update)
    path.write_text(yaml.safe_dump(config))


def test_postgres_adapter_contract(cli, tmp_path: Path, fake_tools) -> None:  # type: ignore[no-untyped-def]
    fake_tools("psql", "pg_dump", "pg_restore", responses={"psql SHOW server_version": (0, "16.2\n")})
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE t (id int);")
    cli("init", cwd=tmp_path)
    cli(
        "env",
        "set",
        PG_ASSIGNMENT,
        cwd=tmp_path,  # highhx:allow-secret
    )  # highhx:allow-secret
    status = cli("db", "status", "--json", cwd=tmp_path).json()
    assert status["reachable"] and status["server_version"] == "16.2"
    assert cli("db", "migrate", "--yes", cwd=tmp_path).code == 0
    assert cli("db", "backup", "--yes", cwd=tmp_path).code == 0
    calls = fake_tools.calls()
    for call in calls:
        assert "pg-Secret-99" not in " ".join(call["argv"]), "password must never appear in argv"
        assert call["env"]["PGPASSWORD"] == "pg-Secret-99"
    psql = [c["argv"] for c in calls if c["tool"] == "psql"]
    assert all(a[3:5] == ["-h", "db.internal"] and a[5:9] == ["-p", "5433", "-U", "app"] for a in psql)
    assert any("--single-transaction" in a for a in psql)
    dump = next(c["argv"] for c in calls if c["tool"] == "pg_dump")
    assert dump[0] == "-Fc" and dump[-1] == "shop"


def test_kubernetes_deploy_and_rollback_contract(cli, tmp_path: Path, fake_tools) -> None:  # type: ignore[no-untyped-def]
    fake_tools("kubectl", responses={"kubectl get": (0, "7")})
    cli("init", cwd=tmp_path)
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "deploy.yaml").write_text("kind: Deployment\n")
    write_config(
        tmp_path,
        {
            "deploy": {
                "default": "prod",
                "targets": {
                    "prod": {
                        "type": "kubernetes",
                        "manifests": "k8s",
                        "deployment": "api",
                        "namespace": "shop",
                        "context": "prod-cluster",
                    }
                },
            }
        },
    )
    (tmp_path / ".highhx" / "policies.yaml").write_text("version: 1\n")
    for version in ("1.0.0", "1.1.0"):
        assert cli("deploy", "--version", version, "--yes", cwd=tmp_path).code == 0
    assert cli("rollback", "--yes", cwd=tmp_path).code == 0
    argvs = [c["argv"] for c in fake_tools.calls()]
    assert ["--context", "prod-cluster", "--namespace", "shop", "apply", "-f", str(tmp_path / "k8s")] in argvs
    assert any(a[4:6] == ["rollout", "status"] and a[6] == "deployment/api" for a in argvs)
    assert any(a[4:6] == ["rollout", "undo"] and "--to-revision=7" in a for a in argvs)


def test_kubernetes_failure_marks_deployment_failed(cli, tmp_path: Path, fake_tools) -> None:  # type: ignore[no-untyped-def]
    fake_tools("kubectl", responses={"kubectl apply": (1, "")})
    cli("init", cwd=tmp_path)
    (tmp_path / "k8s").mkdir()
    write_config(tmp_path, {"deploy": {"targets": {"prod": {"type": "kubernetes", "manifests": "k8s"}}}})
    (tmp_path / ".highhx" / "policies.yaml").write_text("version: 1\n")
    assert cli("deploy", "--version", "1.0.0", "--yes", cwd=tmp_path).code == 1
    history = cli("deploy", "status", "--history", "--json", cwd=tmp_path).json()["deployments"]
    assert history[0]["status"] == "failed"


def test_terraform_contract(cli, tmp_path: Path, fake_tools) -> None:  # type: ignore[no-untyped-def]
    fake_tools("terraform", responses={"terraform output": (0, json.dumps({"url": {"value": "https://x"}}))})
    cli("init", cwd=tmp_path)
    (tmp_path / "infra").mkdir()
    write_config(
        tmp_path,
        {
            "deploy": {
                "targets": {"infra": {"type": "terraform", "directory": "infra", "vars": {"region": "eu-west-1"}}}
            }
        },
    )
    (tmp_path / ".highhx" / "policies.yaml").write_text("version: 1\n")
    denied = cli("deploy", "infra", "--version", "1", cwd=tmp_path)
    assert denied.code == 6 and not fake_tools.calls()
    result = cli("deploy", "infra", "--version", "1", "--yes", "--json", cwd=tmp_path)
    assert result.code == 0 and result.json()["deployment"]["details"]["outputs"] == {"url": "https://x"}
    argvs = [c["argv"] for c in fake_tools.calls()]
    assert argvs[0] == ["init", "-input=false"]
    assert argvs[1][:2] == ["plan", "-input=false"] and argvs[1][3:5] == ["-var", "region=eu-west-1"]
    assert argvs[2] == ["apply", "-input=false", "highhx.tfplan"]


def test_docker_compose_contract(cli, tmp_path: Path, fake_tools) -> None:  # type: ignore[no-untyped-def]
    fake_tools(
        "docker",
        responses={"docker info": (0, "25.0\n"), "docker compose ps": (0, '{"Service":"web","State":"running"}\n')},
    )
    (tmp_path / "compose.yaml").write_text("services: {web: {image: nginx}}\n")
    cli("init", cwd=tmp_path)
    assert cli("docker", "up", "web", "--build", cwd=tmp_path).code == 0
    assert cli("docker", "--json", cwd=tmp_path).json()["services"][0]["running"] is True
    assert cli("docker", "down", "--volumes", cwd=tmp_path).code == 6
    assert cli("docker", "down", cwd=tmp_path).code == 0
    argvs = [c["argv"] for c in fake_tools.calls()]
    assert ["compose", "up", "-d", "--build", "web"] in argvs
    assert ["compose", "down"] in argvs and ["compose", "down", "--volumes"] not in argvs


def test_ssh_deploy_to_unreachable_host_fails_cleanly(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import shutil

    if shutil.which("ssh") is None:
        pytest.skip("ssh not installed")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    cli("init", cwd=tmp_path)
    write_config(
        tmp_path,
        {
            "deploy": {
                "targets": {
                    "box": {
                        "type": "ssh",
                        "host": "127.0.0.1",
                        "port": closed_port,
                        "user": "deploy",
                        "command": "./deploy.sh {{ version }}",
                    }
                }
            }
        },
    )
    (tmp_path / ".highhx" / "policies.yaml").write_text("version: 1\n")
    result = cli("deploy", "box", "--version", "1.0.0", "--yes", cwd=tmp_path)
    assert result.code == 1
    assert "ssh" in result.stderr and "connectivity" in result.stderr
    history = cli("deploy", "status", "--history", "--json", cwd=tmp_path).json()["deployments"]
    assert history[0]["status"] == "failed"


def test_deploy_rejects_injection_in_version(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    marker = tmp_path / "pwned"
    cli("init", cwd=tmp_path)
    write_config(
        tmp_path,
        {
            "deploy": {
                "targets": {"l": {"type": "local", "command": f'"{sys.executable}" -c "print(1)" {{{{ version }}}}'}}
            }
        },
    )
    (tmp_path / ".highhx" / "policies.yaml").write_text("version: 1\n")
    result = cli("deploy", "l", "--version", f"1.0; touch {marker}", "--yes", cwd=tmp_path)
    assert result.code == 8 and not marker.exists()
