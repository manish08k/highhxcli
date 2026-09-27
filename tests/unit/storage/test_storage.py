import threading
import time
from pathlib import Path

import pytest

from highhx.core.errors import NotFoundError
from highhx.observability.metrics import compute_metrics
from highhx.observability.tracing import Tracer, load_trace
from highhx.security.secrets import Redactor
from highhx.storage.cache import StateStore
from highhx.storage.database import Database
from highhx.storage.history import HistoryStore
from highhx.storage.logs import LogStore
from highhx.storage.migrations import LATEST_VERSION, current_version


@pytest.fixture
def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database.open(tmp_path / "state" / "h.db")
    yield database
    database.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    Database.open(path).close()
    db = Database.open(path)
    assert current_version(db) == LATEST_VERSION
    db.close()


def test_history_roundtrip_and_prefix_lookup(db: Database) -> None:
    history = HistoryStore(db, Redactor(["topsecretvalue"]))
    exec_id = history.start("workflow", "ci", command="deploy --token topsecretvalue")
    history.add_step(exec_id, "build", "success", exit_code=0, duration=1.5, outputs={"v": "1"})
    history.finish(exec_id, "success", exit_code=0, duration=2.0)
    record = history.get(exec_id[:-2])
    assert record.id == exec_id and record.status == "success"
    assert record.command == "deploy --token [REDACTED]"
    assert record.steps[0].outputs == {"v": "1"}
    with pytest.raises(NotFoundError):
        history.get("nope")


def test_history_filters_stats_and_stale(db: Database) -> None:
    history = HistoryStore(db)
    for status in ("success", "failed", "success"):
        exec_id = history.start("command", "pytest")
        history.finish(exec_id, status, duration=1.0)
    dead = history.start("command", "orphan", metadata={"pid": 999_999_9})
    db.execute("UPDATE executions SET metadata = ? WHERE id = ?", ('{"pid": 999999999}', dead))
    assert len(history.list(status="failed")) == 1
    stats = {row["name"]: row for row in history.stats()}
    assert stats["pytest"]["runs"] == 3 and stats["pytest"]["failed"] == 1
    assert history.mark_stale_running() == 1
    assert history.get(dead).status == "cancelled"
    metrics = compute_metrics(db)
    assert metrics.total == 4 and metrics.failed == 1


def test_logs_follow_until_finished(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "logs", Redactor(["pw123456"]))
    writer = store.writer("exec-1")
    done = threading.Event()

    def produce() -> None:
        for i in range(3):
            writer.write(f"line {i} pw123456")
            time.sleep(0.05)
        done.set()

    threading.Thread(target=produce).start()
    lines = list(store.follow("exec-1", is_running=lambda: not done.is_set(), poll=0.02))
    assert len(lines) == 3 and all("[REDACTED]" in line for line in lines)
    assert store.read("exec-1", tail=1)[0].endswith("line 2 [REDACTED]")


def test_state_store(db: Database) -> None:
    state = StateStore(db)
    state.set("profile", "staging")
    assert state.get("profile") == "staging"
    state.set("profile", "production")
    assert state.get("profile") == "production"
    state.delete("profile")
    assert state.get("profile", "unset") == "unset"


def test_tracing_builds_tree(db: Database) -> None:
    tracer = Tracer(db)
    with tracer.span("workflow"), tracer.span("step"):
        pass
    roots = load_trace(db, tracer.trace_id)
    assert roots[0].name == "workflow" and roots[0].children[0].name == "step"


def test_concurrent_writes_are_safe(db: Database) -> None:
    history = HistoryStore(db)
    exec_id = history.start("workflow", "p")

    def add(i: int) -> None:
        history.add_step(exec_id, f"s{i}", "success")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(history.get(exec_id).steps) == 20


def test_concurrent_processes_migrate_a_fresh_database_once(tmp_path: Path) -> None:
    """Regression: two HighhX processes opening a new database raced on ALTER TABLE migrations
    ("duplicate column name") and one silently ran without history."""
    import subprocess
    import sys

    db_path = tmp_path / "state" / "highhx.db"
    code = (
        "import sys; from pathlib import Path; from highhx.storage.database import Database; "
        "from highhx.storage.migrations import LATEST_VERSION, current_version; "
        "db = Database.open(Path(sys.argv[1])); assert current_version(db) == LATEST_VERSION; db.close()"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(db_path)], stderr=subprocess.PIPE, text=True)
        for _ in range(8)
    ]
    errors = [p.communicate()[1] for p in procs]
    assert [p.returncode for p in procs] == [0] * 8, errors
    db = Database.open(db_path)
    try:
        versions = [r["version"] for r in db.query("SELECT version FROM schema_version ORDER BY version")]
    finally:
        db.close()
    assert versions == list(range(1, LATEST_VERSION + 1))
