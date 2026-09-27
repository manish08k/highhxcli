"""Capabilities: what this HighhX session can do, Free or Pro.

One interactive CLI serves both plans; this module decides which capabilities it
offers. Local capabilities run entirely on this machine and need no account.
Platform capabilities are the plan features from :mod:`highhx.cloud.plans` — their
values *are* the feature ids — and are granted only when the HighhX platform
reports them for the signed-in account (``/v1/me``).

The result only shapes the CLI (what to offer, what to explain). Authorization for
platform capabilities stays on the platform: the AI gateway checks the account's
plan on every request, so a modified local cache or flag unlocks nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from highhx.cloud import plans
from highhx.core.errors import AccountError, CloudError, HighhXError

if TYPE_CHECKING:
    from highhx.cloud.account import Account, CloudAccount


class Capability(StrEnum):
    # Local: every installation, no account, works offline.
    LOCAL_COMMANDS = "local.commands"
    """Every HighhX command (status, test, build, git, deploy, security …), from the session too."""
    LOCAL_SHELL = "local.shell"
    """`!command`: shell commands through the engine (risk, policy, approval, history)."""
    LOCAL_INTENTS = "local.intents"
    """Plain-language requests that map deterministically to a HighhX command (no AI)."""
    LOCAL_AUTOMATION = "local.automation"
    """Deterministic browser / desktop automation with known targets (`highhx computer`, `highhx do`)."""
    # Platform: plan features, granted by the HighhX platform.
    AI_AGENT = plans.AGENT
    """The AI developer agent via the HighhX gateway: understanding, planning, multi-step work."""
    AI_CODE_CHANGES = plans.AGENT_CODE_CHANGES
    AI_COMMANDS = plans.AGENT_COMMANDS
    AI_GIT = plans.AGENT_GIT
    AI_DEPLOY = plans.AGENT_DEPLOY
    AI_COMPUTER_USE = plans.AGENT_COMPUTER_USE
    CLOUD_SESSIONS = plans.CLOUD_SESSIONS


LOCAL = frozenset(
    {Capability.LOCAL_COMMANDS, Capability.LOCAL_SHELL, Capability.LOCAL_INTENTS, Capability.LOCAL_AUTOMATION}
)
PLATFORM = frozenset(c for c in Capability if c not in LOCAL)

LABELS: dict[Capability, str] = {
    Capability.LOCAL_COMMANDS: "HighhX commands (test, build, git, deploy, security …)",
    Capability.LOCAL_SHELL: "Shell commands with risk checks and approvals (!command)",
    Capability.LOCAL_INTENTS: "Plain-language requests for known actions (no AI)",
    Capability.LOCAL_AUTOMATION: "Deterministic browser and app automation",
    Capability.AI_AGENT: "AI developer agent on hosted models (understanding, planning, multi-step work)",
    Capability.AI_CODE_CHANGES: "AI code changes",
    Capability.AI_COMMANDS: "AI-run tests, builds and commands",
    Capability.AI_GIT: "AI git operations",
    Capability.AI_DEPLOY: "AI-driven deployments",
    Capability.AI_COMPUTER_USE: "AI browser and desktop automation",
    Capability.CLOUD_SESSIONS: "Agent sessions synced to your account",
}


class Connection(StrEnum):
    LOCAL = "local"
    """Not signed in: local capabilities only."""
    CONNECTED = "connected"
    """Signed in; the platform answered."""
    CACHED = "cached"
    """Signed in; the platform is unreachable and only a local copy of the account is known.
    A local copy is not proof of a plan (anyone can edit it), so it grants no platform capability."""
    UNAVAILABLE = "unavailable"
    """Signed in, but the account could not be confirmed (offline, expired sign-in …)."""


@dataclass(frozen=True)
class Entitlements:
    capabilities: frozenset[Capability]
    connection: Connection
    account: Account | None = None
    problem: str | None = None
    """Why platform capabilities are unavailable, when they are expected (signed in)."""

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def tier(self) -> str:
        """The plan to show. Offline, that is the cached plan (labelled as such); it unlocks nothing."""
        if self.has(Capability.AI_AGENT):
            return "Pro"
        if self.connection == Connection.CACHED and self.account is not None and self.account.is_pro:
            return "Pro"
        return "Free"

    @property
    def signed_in(self) -> bool:
        return self.connection != Connection.LOCAL

    @property
    def status(self) -> str:
        """``Free • Local``, ``Pro • Connected``, ``Pro • Offline (cached)``, ``Free • Platform unavailable`` …"""
        state = {
            Connection.LOCAL: "Local",
            Connection.CONNECTED: "Connected",
            Connection.CACHED: "Offline (cached)",
            Connection.UNAVAILABLE: "Platform unavailable",
        }[self.connection]
        return f"{self.tier} • {state}"

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier.lower(),
            "connection": str(self.connection),
            "capabilities": sorted(str(c) for c in self.capabilities),
            "problem": self.problem,
        }


def local() -> Entitlements:
    return Entitlements(LOCAL, Connection.LOCAL)


def from_account(account: Account) -> Entitlements:
    """Capabilities for ``account``: local ones plus the platform features it reports.

    Only an answer from the platform grants platform capabilities. A cached account (the
    platform is unreachable) keeps the session local: without the platform no Pro
    capability can work anyway, and the cache is a local file, not an entitlement.
    """
    if account.cached:
        return Entitlements(
            LOCAL,
            Connection.CACHED,
            account,
            problem="HighhX platform unavailable. Local capabilities remain available.",
        )
    granted = frozenset(c for c in PLATFORM if account.has(c.value))
    return Entitlements(LOCAL | granted, Connection.CONNECTED, account)


def resolve(cloud: CloudAccount) -> Entitlements:
    """This user's capabilities. Never raises: without the platform, local capabilities remain."""
    try:
        if not cloud.signed_in:
            return local()
        account = cloud.account()
    except AccountError as exc:
        return Entitlements(LOCAL, Connection.UNAVAILABLE, problem=exc.message + (f" {exc.hint}" if exc.hint else ""))
    except CloudError:
        return Entitlements(
            LOCAL, Connection.UNAVAILABLE, problem="HighhX platform unavailable. Local capabilities remain available."
        )
    except HighhXError as exc:
        return Entitlements(LOCAL, Connection.UNAVAILABLE, problem=exc.message)
    return from_account(account)
