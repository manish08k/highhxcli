"""Platform test fixtures: an in-memory database and scripted upstream providers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from helpers import PASSWORD, Upstream
from highhx.cloud.protocol import CLIENT_HEADER, PROTOCOL_HEADER, PROTOCOL_VERSION
from highhx_platform.app import create_app
from highhx_platform.config import Settings


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def _postgres_available() -> bool:
    if os.environ.get("HIGHHX_TEST_POSTGRES_URL"):
        return True
    try:
        import embedded_postgres  # noqa: F401
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


BACKENDS = ["sqlite", *(["postgresql"] if _postgres_available() else [])]


@pytest.fixture(scope="session")
def postgres_base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A PostgreSQL server for the test session: HIGHHX_TEST_POSTGRES_URL, or embedded binaries."""
    url = os.environ.get("HIGHHX_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    import embedded_postgres

    server = embedded_postgres.get_server(str(tmp_path_factory.mktemp("pgdata")), cleanup_mode="stop")
    yield server.get_uri("postgres")


def _sqlalchemy_url(uri: str, database: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(("postgresql+psycopg", parsed.netloc, f"/{database}", parsed.query, ""))


@pytest.fixture(params=BACKENDS)
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """Every platform test runs on SQLite and, when available, on a fresh PostgreSQL database."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'platform.db'}"
        return
    import psycopg

    base = request.getfixturevalue("postgres_base")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield _sqlalchemy_url(base, name)
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        public_url="https://platform.test",
        provider_keys={"anthropic": "sk-ant-test", "openai": "sk-openai-test"},
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret="whsec_test",
        stripe_price_pro="price_pro",
        admin_token="admin-secret",  # highhx:allow-secret (test fixture)
        device_poll_interval=0,
        signups_per_hour=1000,
    )


@pytest.fixture
def client(settings: Settings, upstream: Upstream) -> Iterator[TestClient]:
    app = create_app(settings, provider_factory=upstream.factory)
    headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, CLIENT_HEADER: "highhx-cli/test"}
    with TestClient(app, base_url="https://platform.test", headers=headers) as test_client:
        yield test_client
    app.state.db.engine.dispose()


@pytest.fixture
def signup(client: TestClient) -> Callable[..., tuple[str, dict[str, Any]]]:
    counter = iter(range(10_000))

    def _signup(email: str | None = None, *, plan: str | None = None) -> tuple[str, dict[str, Any]]:
        email = email or f"dev{next(counter)}@example.com"
        response = client.post("/v1/auth/signup", json={"email": email, "password": PASSWORD, "name": "Dev"})
        assert response.status_code == 201, response.text
        token = response.json()["access_token"]
        if plan:
            admin = client.post(
                "/v1/admin/plan", json={"email": email, "plan": plan}, headers={"x-admin-token": "admin-secret"}
            )
            assert admin.status_code == 200, admin.text
        return token, response.json()["account"]

    return _signup


@pytest.fixture
def signup_factory() -> Callable[..., str]:
    """Sign up on an arbitrary TestClient (for tests that build their own app)."""
    counter = iter(range(10_000))

    def _signup(test_client: TestClient, *, plan: str | None = None) -> str:
        email = f"user{next(counter)}@example.com"
        response = test_client.post("/v1/auth/signup", json={"email": email, "password": PASSWORD})
        assert response.status_code == 201, response.text
        if plan:
            admin = test_client.post(
                "/v1/admin/plan", json={"email": email, "plan": plan}, headers={"x-admin-token": "admin-secret"}
            )
            assert admin.status_code == 200, admin.text
        return str(response.json()["access_token"])

    return _signup
