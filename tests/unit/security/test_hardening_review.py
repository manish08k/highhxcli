"""Regression tests for the reviewed Bandit findings that were real issues."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from highhx.core.errors import ValidationError
from highhx.deployment.health import http_probe
from highhx.integrations.databases.base import sql_literal
from highhx.project.manifest import parse_xml, read_pom


# ------------------------------------------------------------------ SQL literals
@pytest.mark.parametrize("name", ["0001_init.sql", "0002 add users.sql", "20260927-orders.v2.sql"])
def test_migration_names_become_plain_literals(name: str) -> None:
    assert sql_literal(name) == f"'{name}'"


@pytest.mark.parametrize(
    "name",
    [
        "x'; DROP TABLE users; --",
        "x\\'; DROP TABLE users; --",  # MySQL: backslash escapes defeat quote doubling
        "a\nb",
        "",
        "-leading-dash.sql",
        "x" * 300,
    ],
)
def test_unsafe_migration_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        sql_literal(name)


def test_sqlite_migration_with_hostile_name_cannot_inject(tmp_path: Path) -> None:
    from highhx.integrations.databases.sqlite import SQLiteAdapter

    class Adapter(SQLiteAdapter):
        path = tmp_path / "app.db"  # type: ignore[assignment]

        def __init__(self) -> None:
            pass

    adapter = Adapter()
    with pytest.raises(ValidationError):
        adapter.apply_migration("x'); DROP TABLE t; --", "CREATE TABLE t (id INTEGER);", "ab" * 32)
    with sqlite3.connect(tmp_path / "app.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "t" not in tables  # the script never ran


# ------------------------------------------------------------------ XML
def test_pom_with_entities_is_refused(tmp_path: Path) -> None:
    pom = tmp_path / "pom.xml"
    pom.write_text(
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        "<project><artifactId>&lol2;</artifactId></project>"
    )
    assert read_pom(pom) is None
    xxe = tmp_path / "xxe.xml"
    xxe.write_text('<!DOCTYPE p [<!ENTITY x SYSTEM "file:///etc/passwd">]><project>&x;</project>')
    with pytest.raises(Exception, match="DTD"):
        parse_xml(xxe)


def test_normal_pom_still_parses(tmp_path: Path) -> None:
    pom = tmp_path / "pom.xml"
    pom.write_text(
        '<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<groupId>com.acme</groupId><artifactId>shop</artifactId><version>1.2.0</version></project>"
    )
    info = read_pom(pom)
    assert info is not None and info.get("artifactId") == "shop"


# ------------------------------------------------------------------ health checks
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"])
def test_health_checks_only_speak_http(url: str) -> None:
    ok, message = http_probe(url, timeout=1, expected=200)
    assert not ok and "only http" in message
