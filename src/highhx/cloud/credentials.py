"""Per-user HighhX platform credentials.

Stored in the user config directory (never inside a project) with mode 0600.
``HIGHHX_TOKEN`` overrides the stored token (CI, containers); ``HIGHHX_API_URL``
overrides the platform URL (self-hosted / development backends).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from highhx.core.errors import ConfigError
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.paths import user_config_dir

DEFAULT_API_URL = "https://api.highhx.dev"
CREDENTIALS_FILE = "credentials.json"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_api_url(url: str) -> str:
    """Normalise the platform URL; plain http is only allowed for local development."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"Invalid HighhX platform URL: {url!r}", hint="Use https://host[:port].")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ConfigError(
            f"Refusing to send credentials over plain http to {parsed.hostname}.",
            hint="Use an https:// URL (http is only allowed for localhost).",
        )
    return url.strip().rstrip("/")


def api_url() -> str:
    return validate_api_url(os.environ.get("HIGHHX_API_URL") or stored().api_url or DEFAULT_API_URL)


@dataclass
class Credentials:
    api_url: str | None = None
    token: str | None = None
    account: dict[str, Any] = field(default_factory=dict)
    """Last account document returned by the platform (for offline display and a short grace period)."""
    account_fetched_at: float | None = None

    @property
    def signed_in(self) -> bool:
        return bool(self.token)

    def account_age(self) -> float | None:
        return None if self.account_fetched_at is None else max(0.0, time.time() - self.account_fetched_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_url": self.api_url,
            "token": self.token,
            "account": self.account,
            "account_fetched_at": self.account_fetched_at,
        }


def credentials_path() -> Path:
    return user_config_dir() / CREDENTIALS_FILE


def stored() -> Credentials:
    """Credentials on disk (without the environment override)."""
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Credentials()
    if not isinstance(data, dict):
        return Credentials()
    api, token, account, fetched = (data.get(k) for k in ("api_url", "token", "account", "account_fetched_at"))
    return Credentials(
        api_url=api if isinstance(api, str) else None,
        token=token if isinstance(token, str) else None,
        account=account if isinstance(account, dict) else {},
        account_fetched_at=float(fetched) if isinstance(fetched, int | float) else None,
    )


def load() -> Credentials:
    """Effective credentials: stored ones, with ``HIGHHX_TOKEN`` taking precedence."""
    creds = stored()
    env_token = os.environ.get("HIGHHX_TOKEN")
    if env_token:
        if creds.token != env_token:
            creds.account, creds.account_fetched_at = {}, None
        creds.token = env_token
    return creds


def save(creds: Credentials) -> None:
    atomic_write_text(credentials_path(), json.dumps(creds.to_dict(), indent=2) + "\n", mode=0o600)


def clear() -> bool:
    """Remove stored credentials; True if a file was removed."""
    path = credentials_path()
    if not path.exists():
        return False
    with contextlib.suppress(OSError):
        path.unlink()
    return not path.exists()
