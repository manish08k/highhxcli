"""Real MySQL integration: starts a private, throwaway mysqld (never touches an existing server).

Skipped when the MySQL server binaries are not installed.
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

_CANDIDATES = (
    "/opt/homebrew/opt/mysql@8.4/bin/mysqld",
    "/opt/homebrew/bin/mysqld",
    "/usr/local/bin/mysqld",
    "/usr/sbin/mysqld",
)
MYSQLD = shutil.which("mysqld") or next((p for p in _CANDIDATES if Path(p).exists()), None)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MYSQLD is None or os.name == "nt", reason="mysqld not installed"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def mysql_server(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    assert MYSQLD is not None
    # The client tools sit next to mysqld in a Homebrew keg, but in /usr/bin (with mysqld in
    # /usr/sbin) on Debian/Ubuntu.
    beside = Path(MYSQLD).parent / "mysql"
    found = shutil.which("mysql")
    if not beside.exists() and found is None:
        pytest.skip("the mysql client is not installed")
    bindir = beside.parent if beside.exists() else Path(found or "").parent
    base = tmp_path_factory.mktemp("mysql")
    data = base / "data"
    subprocess.run(
        [MYSQLD, "--no-defaults", "--initialize-insecure", f"--datadir={data}"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    port = free_port()
    # Relative socket path: absolute temp paths exceed the 103-byte UNIX socket limit on macOS.
    server = subprocess.Popen(
        [
            MYSQLD,
            "--no-defaults",
            f"--datadir={data}",
            "--socket=s",
            f"--port={port}",
            "--bind-address=127.0.0.1",
            "--mysqlx=OFF",
            f"--pid-file={base / 'pid'}",
        ],
        cwd=base,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = [str(bindir / "mysql"), "--no-defaults", "-uroot", "-h127.0.0.1", f"-P{port}", "--protocol=TCP"]
    deadline = time.monotonic() + 60
    while subprocess.run([*client, "-e", "SELECT 1"], capture_output=True, check=False).returncode != 0:
        if time.monotonic() > deadline or server.poll() is not None:
            server.kill()
            pytest.skip("private mysqld did not start")
        time.sleep(0.5)
    setup = "CREATE DATABASE app; CREATE USER 'app'@'%' IDENTIFIED BY 'Pa$$w0rd!x'; GRANT ALL ON app.* TO 'app'@'%';"
    subprocess.run([*client, "-e", setup], check=True)
    yield {"port": port, "client": client, "bindir": bindir}
    server.terminate()
    try:
        server.wait(30)
    except subprocess.TimeoutExpired:
        server.kill()


def test_mysql_full_lifecycle(cli, tmp_path: Path, mysql_server, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PATH", f"{mysql_server['bindir']}{os.pathsep}{os.environ['PATH']}")
    tmp_path = tmp_path / "my project"  # backups and restores must cope with spaces in paths
    tmp_path.mkdir()
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_users.sql").write_text(
        "CREATE TABLE users (id INT PRIMARY KEY AUTO_INCREMENT, email VARCHAR(200));"
    )
    (tmp_path / "migrations" / "002_name.sql").write_text("ALTER TABLE users ADD COLUMN name VARCHAR(100);")
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "01.sql").write_text("INSERT INTO users (email, name) VALUES ('a@example.com', 'Ann');")
    cli("init", cwd=tmp_path)
    url = f"mysql://app:Pa%24%24w0rd%21x@127.0.0.1:{mysql_server['port']}/app"  # highhx:allow-secret
    assert cli("env", "set", f"DATABASE_URL={url}", cwd=tmp_path).code == 0
    status = cli("db", "status", "--json", cwd=tmp_path)
    assert status.code == 0 and status.json()["server_version"].startswith("8")
    assert "Pa$$w0rd" not in status.stdout
    assert cli("db", "migrate", "--yes", "--json", cwd=tmp_path).json()["applied"] == ["001_users", "002_name"]
    assert cli("db", "migrate", "--yes", "--json", cwd=tmp_path).json()["applied"] == []
    assert cli("db", "seed", "--yes", cwd=tmp_path).code == 0
    backup = cli("db", "backup", "--yes", cwd=tmp_path)
    assert backup.code == 0 and "PROCESS privilege" not in backup.stdout + backup.stderr
    subprocess.run([*mysql_server["client"], "app", "-e", "DELETE FROM users"], check=True)
    assert cli("db", "restore", "--yes", cwd=tmp_path).code == 0
    rows = subprocess.run(
        [*mysql_server["client"], "app", "-N", "-e", "SELECT email FROM users"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert rows == ["a@example.com"]
    for path in (tmp_path / ".highhx").rglob("*"):
        if path.is_file() and path.suffix != ".sql":
            assert b"Pa$$w0rd" not in path.read_bytes()


def test_mysql_bad_credentials_are_reported(cli, tmp_path: Path, mysql_server, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PATH", f"{mysql_server['bindir']}{os.pathsep}{os.environ['PATH']}")
    cli("init", cwd=tmp_path)
    cli("env", "set", f"DATABASE_URL=mysql://app:wrong@127.0.0.1:{mysql_server['port']}/app", cwd=tmp_path)
    result = cli("db", "status", cwd=tmp_path)
    assert result.code == 1 and "Access denied" in result.stdout + result.stderr
