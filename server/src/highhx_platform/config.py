"""Platform configuration, read from the environment (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


@dataclass
class Settings:
    database_url: str = "sqlite:///./highhx-platform.db"
    public_url: str = "http://localhost:8080"
    """Base URL users reach the platform at (device sign-in page, checkout return URLs)."""
    signup_enabled: bool = True
    default_provider: str = "anthropic"
    provider_keys: dict[str, str] = field(default_factory=dict)
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_pro: str | None = None
    admin_token: str | None = None
    max_request_bytes: int = 8_000_000
    max_output_tokens: int = 64_000
    login_attempts_per_15min: int = 10
    signups_per_hour: int = 5
    device_starts_per_hour: int = 30
    device_code_ttl: int = 900
    device_poll_interval: int = 5
    token_ttl_days: int = 90
    """CLI/API tokens expire after this many days (refresh with POST /v1/auth/refresh)."""
    max_concurrent_streams: int = 3
    """Simultaneous AI gateway requests per account."""
    stream_resume_grace: float = 30.0
    """Seconds an AI stream keeps running without a connected client before it is cancelled."""
    stream_retention: float = 600.0
    """Seconds a finished stream stays replayable for reconnecting clients."""
    fallback_providers: tuple[str, ...] = ()
    """Upstreams to try, in order, when the chosen one fails before producing output."""
    cors_origins: tuple[str, ...] = ()
    auto_migrate: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        keys = {
            name: value
            for name, env in (
                ("anthropic", "ANTHROPIC_API_KEY"),
                ("openai", "OPENAI_API_KEY"),
                ("gemini", "GEMINI_API_KEY"),
            )
            if (value := os.environ.get(env))
        }
        return cls(
            database_url=os.environ.get("HIGHHX_DATABASE_URL", cls.database_url),
            public_url=os.environ.get("HIGHHX_PUBLIC_URL", cls.public_url).rstrip("/"),
            signup_enabled=_bool("HIGHHX_SIGNUP_ENABLED", True),
            default_provider=os.environ.get("HIGHHX_DEFAULT_PROVIDER", cls.default_provider),
            provider_keys=keys,
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY") or None,
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
            stripe_price_pro=os.environ.get("STRIPE_PRICE_PRO") or None,
            admin_token=os.environ.get("HIGHHX_ADMIN_TOKEN") or None,
            token_ttl_days=int(os.environ.get("HIGHHX_TOKEN_TTL_DAYS", "90")),
            max_concurrent_streams=int(os.environ.get("HIGHHX_MAX_CONCURRENT_STREAMS", "3")),
            fallback_providers=_list("HIGHHX_FALLBACK_PROVIDERS"),
            cors_origins=_list("HIGHHX_CORS_ORIGINS"),
            auto_migrate=_bool("HIGHHX_AUTO_MIGRATE", True),
        )

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_pro)
