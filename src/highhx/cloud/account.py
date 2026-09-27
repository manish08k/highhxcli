"""The signed-in HighhX account: sign-in flows, plan entitlements and platform calls."""

from __future__ import annotations

import platform as _platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from highhx import __version__
from highhx.cloud import credentials
from highhx.cloud.client import PlatformClient
from highhx.cloud.plans import PRO, Plan, plan_for
from highhx.core.errors import AccountError, CloudError, OperationCancelledError, PlanRequiredError
from highhx.execution.cancellation import CancellationToken

OFFLINE_GRACE_SECONDS = 24 * 3600
"""How long a cached account document may stand in when the platform is unreachable."""


@dataclass
class Account:
    id: str
    email: str
    name: str
    plan: Plan
    features: frozenset[str]
    usage: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    subscription: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    """True when this came from the local cache because the platform was unreachable."""

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, cached: bool = False) -> Account:
        raw_user = data.get("user")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else data
        plan = plan_for(str(data.get("plan") or ""))
        features = data.get("features")
        return cls(
            id=str(user.get("id") or ""),
            email=str(user.get("email") or ""),
            name=str(user.get("name") or ""),
            plan=plan,
            # The platform's feature list is authoritative; fall back to the plan definition.
            features=frozenset(str(f) for f in features) if isinstance(features, list) else plan.features,
            usage=dict(data.get("usage") or {}),
            settings=dict(data.get("settings") or {}),
            subscription=dict(data.get("subscription") or {}),
            cached=cached,
        )

    @property
    def is_pro(self) -> bool:
        return self.plan.id == PRO

    def has(self, feature: str) -> bool:
        return feature in self.features

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": {"id": self.id, "email": self.email, "name": self.name},
            "plan": self.plan.id,
            "plan_name": self.plan.name,
            "features": sorted(self.features),
            "usage": self.usage,
            "settings": self.settings,
            "subscription": self.subscription,
            "cached": self.cached,
        }


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: float
    interval: float


class CloudAccount:
    """Facade over the platform API for the current user."""

    def __init__(self, client_factory: Callable[[str, str | None], PlatformClient] | None = None) -> None:
        self._client_factory = client_factory or PlatformClient
        self._account: Account | None = None

    # ---------------------------------------------------------------- basics
    @property
    def credentials(self) -> credentials.Credentials:
        return credentials.load()

    @property
    def api_url(self) -> str:
        return credentials.api_url()

    @property
    def signed_in(self) -> bool:
        return self.credentials.signed_in

    def client(self, *, authenticated: bool = True) -> PlatformClient:
        token = self.credentials.token if authenticated else None
        if authenticated and not token:
            raise AccountError("You are not signed in to HighhX.", hint="Run `highhx login`.")
        return self._client_factory(self.api_url, token)

    # --------------------------------------------------------------- account
    def account(self, *, refresh: bool = True) -> Account:
        """The signed-in account. Falls back to a recent cached copy when offline."""
        if self._account is not None and not refresh:
            return self._account
        creds = self.credentials
        if not creds.signed_in:
            raise AccountError("You are not signed in to HighhX.", hint="Run `highhx login`.")
        try:
            data = self.client().get("/v1/me")
        except CloudError:
            age = creds.account_age()
            if creds.account and age is not None and age < OFFLINE_GRACE_SECONDS:
                self._account = Account.from_dict(creds.account, cached=True)
                return self._account
            raise
        stored = credentials.stored()
        if stored.token == creds.token:
            stored.account, stored.account_fetched_at = data, time.time()
            credentials.save(stored)
        self._account = Account.from_dict(data)
        return self._account

    def require(self, feature: str, *, what: str = "This feature") -> Account:
        """The account, raising unless its plan includes ``feature``."""
        if not self.signed_in:
            raise AccountError(
                f"{what} is part of HighhX Pro — sign in first.",
                hint="Run `highhx login` (new here? `highhx login` also creates your account).",
            )
        account = self.account()
        if not account.has(feature):
            raise PlanRequiredError(
                f"{what} requires HighhX Pro (you are on {account.plan.name}).",
                hint="Upgrade with `highhx account upgrade` — everything else in HighhX stays free.",
            )
        return account

    # --------------------------------------------------------------- sign-in
    def start_device_login(self) -> DeviceCode:
        data = self.client(authenticated=False).post(
            "/v1/auth/device",
            {"client": "highhx-cli", "version": __version__, "hostname": _platform.node() or "unknown"},
        )
        try:
            return DeviceCode(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(data["verification_uri"]),
                verification_uri_complete=str(data.get("verification_uri_complete") or data["verification_uri"]),
                expires_in=float(data.get("expires_in") or 900),
                interval=max(1.0, float(data.get("interval") or 5)),
            )
        except (KeyError, TypeError, ValueError):
            raise CloudError("The HighhX platform returned an invalid sign-in response.") from None

    def poll_device_login(
        self,
        code: DeviceCode,
        *,
        cancel: CancellationToken | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Account:
        """Wait until the user approves the code in the browser, then store the token."""
        client = self.client(authenticated=False)
        deadline = time.monotonic() + code.expires_in
        interval = code.interval
        while time.monotonic() < deadline:
            if cancel is not None and cancel.cancelled:
                raise OperationCancelledError("Sign-in cancelled.")
            data = client.post("/v1/auth/device/token", {"device_code": code.device_code})
            status = data.get("status")
            if status == "approved" and data.get("access_token"):
                return self._store_token(str(data["access_token"]))
            if status == "denied":
                raise AccountError("Sign-in was denied in the browser.")
            if status == "expired":
                break
            if status == "slow_down":
                interval += 5
            sleep(interval)
        raise AccountError("The sign-in code expired before it was approved.", hint="Run `highhx login` again.")

    def login_with_token(self, token: str) -> Account:
        """Sign in with an API token created on the platform (CI, headless machines)."""
        token = token.strip()
        if not token:
            raise AccountError("The token is empty.")
        return self._store_token(token)

    def _store_token(self, token: str) -> Account:
        data = self._client_factory(self.api_url, token).get("/v1/me")
        creds = credentials.stored()
        creds.token = token
        creds.api_url = self.api_url
        creds.account, creds.account_fetched_at = data, time.time()
        credentials.save(creds)
        self._account = Account.from_dict(data)
        return self._account

    def logout(self) -> bool:
        """Revoke the token on the platform (best effort) and forget it locally."""
        creds = credentials.stored()
        if creds.token:
            try:
                self._client_factory(self.api_url, creds.token).post("/v1/auth/logout")
            except (CloudError, AccountError):
                pass
        self._account = None
        return credentials.clear()

    # ----------------------------------------------------------- platform api
    def usage(self) -> dict[str, Any]:
        return self.client().get("/v1/usage")

    def checkout_url(self, plan: str = PRO) -> str:
        data = self.client().post("/v1/billing/checkout", {"plan": plan})
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CloudError("The HighhX platform did not return a checkout link.")
        return url

    def billing_portal_url(self) -> str:
        data = self.client().post("/v1/billing/portal")
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CloudError("The HighhX platform did not return a billing portal link.")
        return url

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        data = self.client().patch("/v1/me/settings", changes)
        self._account = None
        return data
