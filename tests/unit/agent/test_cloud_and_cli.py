"""Platform client, credentials, entitlements, memory/settings, and the Pro/account CLI commands."""

from __future__ import annotations

import io
import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.memory import ProjectMemory
from highhx.agent.permissions import ApprovalMode
from highhx.agent.settings import AgentSettings
from highhx.cloud import credentials
from highhx.cloud.account import Account, CloudAccount, DeviceCode
from highhx.cloud.client import http_error
from highhx.cloud.plans import AGENT, FREE, PLANS, PRO, plan_for
from highhx.config.validation import validate_config
from highhx.core.errors import AccountError, CloudError, ConfigError, PlanRequiredError, QuotaExceededError


def account_doc(plan: str = PRO, **extra: Any) -> dict[str, Any]:
    return {
        "user": {"id": "u1", "email": "dev@example.com", "name": "Dev"},
        "plan": plan,
        "features": sorted(PLANS[plan].features),
        "usage": {"tokens_used": 1200, "tokens_included": PLANS[plan].monthly_tokens},
        "settings": {},
        **extra,
    }


class FakeClient:
    """Stands in for PlatformClient; routes are canned responses or exceptions."""

    calls: list[tuple[str, str, Any, str | None]] = []
    routes: dict[tuple[str, str], Any] = {}

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url, self.token = base_url, token

    def _answer(self, method: str, path: str, body: Any) -> dict[str, Any]:
        FakeClient.calls.append((method, path, body, self.token))
        answer = FakeClient.routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(body, self.token)
        if answer is None:
            raise CloudError(f"no fake route for {method} {path}")
        return answer

    def get(self, path: str) -> dict[str, Any]:
        return self._answer("GET", path, None)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._answer("POST", path, body)

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._answer("PATCH", path, body)


@pytest.fixture
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.calls, FakeClient.routes = [], {}
    monkeypatch.setattr("highhx.cloud.account.PlatformClient", FakeClient)
    monkeypatch.delenv("HIGHHX_TOKEN", raising=False)
    monkeypatch.delenv("HIGHHX_API_URL", raising=False)
    return FakeClient


# ------------------------------------------------------------------ plans
def test_plans_make_the_free_pro_split_explicit() -> None:
    assert AGENT not in PLANS[FREE].features and AGENT in PLANS[PRO].features
    assert PLANS[FREE].features <= PLANS[PRO].features
    assert plan_for("enterprise-unknown").id == FREE


# ------------------------------------------------------------ credentials
def test_credentials_are_private_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    stored_token = "hhx_stored"  # highhx:allow-secret (test fixture)
    credentials.save(credentials.Credentials(token=stored_token, account={"plan": PRO}, account_fetched_at=time.time()))
    path = credentials.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.load().token == "hhx_stored"
    monkeypatch.setenv("HIGHHX_TOKEN", "hhx_env")
    loaded = credentials.load()
    assert loaded.token == "hhx_env" and loaded.account == {}
    assert credentials.clear() and not path.exists()


def test_api_url_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert credentials.validate_api_url("https://api.example.com/") == "https://api.example.com"
    assert credentials.validate_api_url("http://localhost:8080") == "http://localhost:8080"
    with pytest.raises(ConfigError, match="plain http"):
        credentials.validate_api_url("http://evil.example.com")
    with pytest.raises(ConfigError):
        credentials.validate_api_url("ftp://x")
    monkeypatch.setenv("HIGHHX_API_URL", "http://127.0.0.1:9999")
    assert credentials.api_url() == "http://127.0.0.1:9999"


def test_http_errors_map_to_actionable_errors() -> None:
    assert isinstance(http_error(401, {}, "u"), AccountError)
    assert isinstance(http_error(402, {"code": "plan_required", "message": "m"}, "u"), PlanRequiredError)
    assert isinstance(http_error(429, {"code": "quota_exceeded", "message": "m"}, "u"), QuotaExceededError)
    server = http_error(503, {"message": "down"}, "u")
    assert isinstance(server, CloudError) and "503" in server.message


# ---------------------------------------------------------------- account
def test_require_gates_features(fake_platform: type[FakeClient]) -> None:
    cloud = CloudAccount()
    with pytest.raises(AccountError, match="sign in"):
        cloud.require(AGENT)
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    with pytest.raises(PlanRequiredError, match="requires HighhX Pro"):
        cloud.require(AGENT, what="The agent")
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    account = cloud.require(AGENT)
    assert account.is_pro and account.email == "dev@example.com"
    assert credentials.stored().account["plan"] == PRO


def test_offline_grace_uses_recent_cache_only(fake_platform: type[FakeClient]) -> None:
    fake_platform.routes[("GET", "/v1/me")] = CloudError("offline")
    credentials.save(
        credentials.Credentials(token="hhx_t", account=account_doc(PRO), account_fetched_at=time.time() - 60)
    )
    account = CloudAccount().account()
    assert account.cached and account.is_pro
    credentials.save(
        credentials.Credentials(token="hhx_t", account=account_doc(PRO), account_fetched_at=time.time() - 3 * 86400)
    )
    with pytest.raises(CloudError):
        CloudAccount().account()


def test_device_login_polls_until_approved(fake_platform: type[FakeClient]) -> None:
    answers = iter([{"status": "pending"}, {"status": "slow_down"}, {"status": "approved", "access_token": "hhx_new"}])
    fake_platform.routes[("POST", "/v1/auth/device")] = {
        "device_code": "dc",
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://app/device",
        "expires_in": 600,
        "interval": 1,
    }
    fake_platform.routes[("POST", "/v1/auth/device/token")] = lambda body, token: next(answers)
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    cloud = CloudAccount()
    code = cloud.start_device_login()
    assert code.user_code == "BCDF-GHJK" and code.verification_uri_complete == "https://app/device"
    sleeps: list[float] = []
    account = cloud.poll_device_login(code, sleep=sleeps.append)
    assert account.is_pro and credentials.stored().token == "hhx_new"
    assert sleeps == [1.0, 6.0]
    assert fake_platform.calls[-1][3] == "hhx_new"


def test_device_login_denied_and_expired(fake_platform: type[FakeClient]) -> None:
    code = DeviceCode("dc", "X", "https://a", "https://a", 600, 1)
    fake_platform.routes[("POST", "/v1/auth/device/token")] = {"status": "denied"}
    with pytest.raises(AccountError, match="denied"):
        CloudAccount().poll_device_login(code, sleep=lambda s: None)
    fake_platform.routes[("POST", "/v1/auth/device/token")] = {"status": "expired"}
    with pytest.raises(AccountError, match="expired"):
        CloudAccount().poll_device_login(code, sleep=lambda s: None)


def test_logout_revokes_and_forgets(fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("POST", "/v1/auth/logout")] = {"revoked": True}
    assert CloudAccount().logout()
    assert ("POST", "/v1/auth/logout", None, "hhx_t") in fake_platform.calls
    assert not CloudAccount().signed_in


def test_account_document_features_are_authoritative() -> None:
    account = Account.from_dict({**account_doc(PRO), "features": ["cli", "agent"]})
    assert account.has("agent") and not account.has("agent.deploy")


# ---------------------------------------------------------- memory/settings
def test_project_memory(tmp_path: Path) -> None:
    memory = ProjectMemory.for_project(tmp_path, initialized=True)
    assert memory.path == tmp_path / ".highhx" / "memory.md"
    assert memory.add("Use `make test`").startswith("Saved")
    assert memory.add("use `make test`") == "Already in project memory."
    assert memory.facts() == ["Use `make test`"]
    memory.redactor.add(["topsecretvalue99"])
    assert "secret" in memory.add("the key is topsecretvalue99")
    elsewhere = ProjectMemory.for_project(tmp_path / "other", initialized=False)
    assert ".highhx" not in str(elsewhere.path)


def test_settings_precedence() -> None:
    settings = AgentSettings.resolve(
        account={"provider": "openai", "model": "gpt-5", "approval": "auto-edit"},
        project={"model": "gpt-5-mini", "max_steps": 200, "instructions": "Be terse."},
        overrides={"approval": "read-only", "provider": None},
        plan_max_steps=60,
    )
    assert settings.provider == "openai" and settings.model == "gpt-5-mini"
    assert settings.approval == ApprovalMode.READ_ONLY and settings.max_steps == 60
    assert settings.instructions == "Be terse."


def test_agent_config_section_is_validated() -> None:
    assert (
        validate_config({"version": 1, "agent": {"provider": "anthropic", "approval": "auto-edit", "max_steps": 20}})
        == []
    )
    errors = validate_config({"version": 1, "agent": {"provider": "skynet", "unknown": 1}})
    assert any("agent.provider" in e for e in errors) and any("agent.unknown" in e for e in errors)


# ---------------------------------------------------------------- commands
def test_agent_without_account_explains_pro(cli, tmp_path: Path) -> None:
    result = cli("agent", "fix my tests", cwd=tmp_path)
    assert result.code == 10
    assert "HighhX Pro" in result.stderr and "HighhX Free" in result.stderr
    assert "highhx login" in result.stderr


def test_agent_json_without_account(cli, tmp_path: Path) -> None:
    result = cli("agent", "hi", "--json", cwd=tmp_path)
    assert result.code == 10
    assert result.json()["error"] == "account"


def test_agent_on_free_plan_is_gated(cli, tmp_path: Path, fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(FREE)
    result = cli("agent", "hi", cwd=tmp_path)
    assert result.code == 10 and "requires HighhX Pro" in result.stderr and "(your plan)" in result.stderr


def test_account_status_signed_out(cli, tmp_path: Path) -> None:
    result = cli("account", "--json", cwd=tmp_path)
    assert result.code == 0 and result.json()["signed_in"] is False
    human = cli("account", cwd=tmp_path)
    assert "HighhX Free" in human.stdout


def test_account_status_signed_in(cli, tmp_path: Path, fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    result = cli("account", "status", cwd=tmp_path)
    assert result.code == 0 and "dev@example.com" in result.stdout and "highhx agent" in result.stdout
    data = cli("account", "--json", cwd=tmp_path).json()
    assert data["plan"] == PRO and data["signed_in"] is True


def test_account_plans_needs_no_account(cli, tmp_path: Path) -> None:
    result = cli("account", "plans", "--json", cwd=tmp_path)
    assert [p["id"] for p in result.json()["plans"]] == [FREE, PRO]


def test_account_settings_round_trip(cli, tmp_path: Path, fake_platform: type[FakeClient]) -> None:
    credentials.save(credentials.Credentials(token="hhx_t"))
    fake_platform.routes[("PATCH", "/v1/me/settings")] = lambda body, token: {"agent": body["agent"]}
    result = cli("account", "settings", "--provider", "openai", "--model", "gpt-5", "--json", cwd=tmp_path)
    assert result.code == 0 and result.json() == {"agent": {"provider": "openai", "model": "gpt-5"}}


def test_login_with_token(
    cli, tmp_path: Path, fake_platform: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_platform.routes[("GET", "/v1/me")] = account_doc(PRO)
    monkeypatch.setattr("sys.stdin", io.StringIO("hhx_ci_token\n"))
    result = cli("login", "--with-token", "--json", cwd=tmp_path)
    assert result.code == 0 and result.json()["plan"] == PRO
    assert credentials.stored().token == "hhx_ci_token"
    assert "hhx_ci_token" not in result.stdout


def test_login_rejects_non_local_http(cli, tmp_path: Path) -> None:
    result = cli("login", "--with-token", "--api-url", "http://example.com", cwd=tmp_path)
    assert result.code == 3 and "plain http" in result.stderr


def test_logout_command(cli, tmp_path: Path, fake_platform: type[FakeClient]) -> None:
    assert json.loads(cli("logout", "--json", cwd=tmp_path).stdout) == {"signed_out": False}


def test_agent_models_lists_providers(cli, tmp_path: Path) -> None:
    rows = {r["provider"]: r for r in cli("agent", "models", "--json", cwd=tmp_path).json()["providers"]}
    assert set(rows) == {"highhx", "anthropic", "openai", "gemini"}
    assert all(r["access"].startswith("HighhX gateway") for r in rows.values())


def test_agent_sessions_empty(cli, tmp_path: Path) -> None:
    assert cli("agent", "sessions", "--json", cwd=tmp_path).json() == {"sessions": []}


def test_bare_highhx_guides_new_users(cli, tmp_path: Path) -> None:
    result = cli(cwd=tmp_path)
    assert result.code == 0
    assert "HighhX Pro — AI developer agent" in result.stdout
    assert "highhx init" in result.stdout and "highhx login" in result.stdout


def test_direct_provider_choice_still_goes_through_the_gateway(
    cli, tmp_path: Path, fake_platform: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no client-side bring-your-own-key path: a provider choice is only a routing
    preference for the platform, which authenticates, entitles and meters every request."""
    from highhx.agent.bootstrap import build_provider
    from highhx.agent.model.platform import PlatformProvider
    from highhx.agent.settings import AgentSettings

    unused_key = "sk-ant-not-used-by-the-cli-0000000000"  # highhx:allow-secret (test fixture)
    monkeypatch.setenv("ANTHROPIC_API_KEY", unused_key)
    credentials.save(credentials.Credentials(token="hhx_t"))
    provider = build_provider(AgentSettings(provider="anthropic"), CloudAccount(), Account.from_dict(account_doc(PRO)))
    assert isinstance(provider, PlatformProvider) and provider.upstream == "anthropic"
