import contextlib
import http.server
import sqlite3
import threading
from pathlib import Path

import pytest

from highhx.config.schema import DatabaseConfig, HealthCheckConfig, HighhXConfig
from highhx.core.errors import ApprovalDeniedError, CommandFailedError, IntegrationError, NotFoundError, ValidationError
from highhx.database.manager import DatabaseManager
from highhx.deployment.health import run_health_check
from highhx.deployment.manager import DeploymentManager
from highhx.deployment.state import DeploymentStore, DeployStatus
from highhx.deployment.target import render_command
from highhx.integrations.databases.url import parse_database_url
from tests.conftest import PY


def deploy_config(tmp_path: Path, **overrides) -> HighhXConfig:  # type: ignore[no-untyped-def]
    marker = tmp_path / "deployed.txt"
    target = {
        "type": "local",
        "command": f"\"{PY}\" -c \"open(r'{marker}', 'w').write('{{{{ version }}}}')\"",
        "rollback_command": f"\"{PY}\" -c \"open(r'{marker}', 'w').write('{{{{ previous_version }}}}')\"",
        **overrides,
    }
    return HighhXConfig.from_dict({"deploy": {"default": "local", "targets": {"local": target}}})


def manager(make_engine, tmp_path: Path, config: HighhXConfig, **engine_kwargs):  # type: ignore[no-untyped-def]
    kit = make_engine(cwd=tmp_path, **engine_kwargs)
    return DeploymentManager(kit.engine, tmp_path, config, DeploymentStore(kit.db)), kit


def test_deploy_and_rollback_flow(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dm, _ = manager(make_engine, tmp_path, deploy_config(tmp_path), yes=True, interactive=False)
    assert dm.deploy(version="1.0.0").ok
    assert dm.deploy(version="1.1.0").ok
    assert (tmp_path / "deployed.txt").read_text() == "1.1.0"
    outcome = dm.rollback()
    assert outcome.ok and (tmp_path / "deployed.txt").read_text() == "1.0.0"
    statuses = [r.status for r in dm.history()]
    assert statuses == [DeployStatus.SUCCEEDED, DeployStatus.ROLLED_BACK, DeployStatus.SUCCEEDED]
    assert dm.history()[0].rollback_of is not None


def test_failed_command_is_recorded_as_failed(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    config = deploy_config(tmp_path, command=f'"{PY}" -c "raise SystemExit(4)"')
    dm, _ = manager(make_engine, tmp_path, config, yes=True, interactive=False)
    with pytest.raises(CommandFailedError):
        dm.deploy(version="1.0.0")
    assert dm.history()[0].status == DeployStatus.FAILED


def test_health_check_failure_and_auto_rollback(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    flag = tmp_path / "healthy"
    config = deploy_config(
        tmp_path,
        health_check={
            "command": f'"{PY}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(r\'{flag}\').exists() else 1)"',
            "retries": 2,
            "interval": 0,
        },
        auto_rollback=True,
    )
    dm, _ = manager(make_engine, tmp_path, config, yes=True, interactive=False)
    flag.write_text("ok")
    assert dm.deploy(version="1.0.0").ok
    flag.unlink()
    outcome = dm.deploy(version="2.0.0")
    assert not outcome.ok and outcome.rolled_back
    assert outcome.record is not None and outcome.record.status == DeployStatus.ROLLED_BACK


def test_production_requires_interactive_confirmation(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from highhx.policy.engine import PolicySet

    policy = PolicySet.from_dict(
        {
            "rules": [
                {
                    "id": "prod",
                    "when": {"action": "deploy:*", "production": True},
                    "effect": "require_approval",
                    "risk": "critical",
                    "bypassable": False,
                }
            ]
        }
    )
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False, policy=policy)
    dm = DeploymentManager(kit.engine, tmp_path, deploy_config(tmp_path, production=True), DeploymentStore(kit.db))
    with pytest.raises(ApprovalDeniedError):
        dm.deploy(version="1.0.0")
    assert not (tmp_path / "deployed.txt").exists()


def test_preflight_and_rollback_errors(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    config = deploy_config(tmp_path, preflight=[f'"{PY}" -c "raise SystemExit(1)"'])
    dm, _ = manager(make_engine, tmp_path, config, yes=True, interactive=False)
    with pytest.raises(ValidationError):
        dm.deploy(version="1")
    with pytest.raises(NotFoundError):
        dm.rollback()
    with pytest.raises(NotFoundError):
        dm.target("nope")


def test_dry_run_deploy(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dm, _ = manager(make_engine, tmp_path, deploy_config(tmp_path), dry_run=True)
    assert dm.deploy(version="1").dry_run
    assert not (tmp_path / "deployed.txt").exists() and dm.history() == []


def test_http_health_check(make_engine) -> None:  # type: ignore[no-untyped-def]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/ok" else 503)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    kit = make_engine()
    try:
        assert run_health_check(kit.engine, HealthCheckConfig(url=f"http://127.0.0.1:{port}/ok", retries=1)).ok
        bad = run_health_check(kit.engine, HealthCheckConfig(url=f"http://127.0.0.1:{port}/bad", retries=2, interval=0))
        assert not bad.ok and bad.attempts == 2 and "503" in bad.message
    finally:
        server.shutdown()
        server.server_close()


def test_render_command() -> None:
    assert (
        render_command("deploy {{ version }} to {{target}}", {"version": "1", "target": "prod"}) == "deploy 1 to prod"
    )
    with pytest.raises(ValueError):
        render_command("{{ previous_version }}", {"previous_version": None})


def test_database_urls() -> None:
    pg = parse_database_url("postgresql+psycopg://user:p%40ss@db:5433/app?sslmode=require")  # highhx:allow-secret
    assert (pg.kind, pg.host, pg.port, pg.user, pg.password, pg.database) == (
        "postgresql",
        "db",
        5433,
        "user",
        "p@ss",
        "app",
    )
    assert "p@ss" not in pg.masked()
    assert parse_database_url("sqlite:///data/app.db").path == "data/app.db"
    assert parse_database_url("sqlite:////abs/app.db").path == "/abs/app.db"
    with pytest.raises(ValidationError):
        parse_database_url("mongodb://x")


def test_sqlite_migrate_seed_backup_restore(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);")
    (tmp_path / "migrations" / "002_more.sql").write_text("ALTER TABLE t ADD COLUMN w TEXT;")
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "01.sql").write_text("INSERT INTO t (v) VALUES ('seeded');")
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    db = DatabaseManager(kit.engine, tmp_path, DatabaseConfig(), {"DATABASE_URL": "sqlite:///app.db"})
    assert db.migrate() == ["001_init", "002_more"]
    assert db.migrate() == []
    assert db.status().migrations == {"applied": ["001_init", "002_more"], "pending": [], "changed": [], "unknown": []}
    db.seed()
    backup = db.backup()
    with contextlib.closing(sqlite3.connect(tmp_path / "app.db")) as conn, conn:
        conn.execute("DELETE FROM t")
    source, safety = db.restore(backup.name)
    assert source == backup and safety is not None
    with contextlib.closing(sqlite3.connect(tmp_path / "app.db")) as conn, conn:
        assert conn.execute("SELECT v FROM t").fetchall() == [("seeded",)]
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE t (id INTEGER);")
    with pytest.raises(ValidationError):
        db.migrate()


def test_failed_migration_rolls_back(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_bad.sql").write_text("CREATE TABLE ok (id INTEGER); THIS IS NOT SQL;")
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    db = DatabaseManager(kit.engine, tmp_path, DatabaseConfig(), {"DATABASE_URL": "sqlite:///app.db"})
    with pytest.raises(IntegrationError):
        db.migrate()
    with contextlib.closing(sqlite3.connect(tmp_path / "app.db")) as conn, conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables


def test_restore_needs_approval(make_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "app.db").write_bytes(b"")
    kit = make_engine(cwd=tmp_path, interactive=False)
    db = DatabaseManager(kit.engine, tmp_path, DatabaseConfig(), {"DATABASE_URL": "sqlite:///app.db"})
    (tmp_path / ".highhx" / "backups").mkdir(parents=True)
    (tmp_path / ".highhx" / "backups" / "x.sqlite3").write_bytes(b"")
    with pytest.raises(ApprovalDeniedError):
        db.restore()
