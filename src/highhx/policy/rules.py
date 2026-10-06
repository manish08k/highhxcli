"""Policy rule model and matching."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from highhx.approvals.risk import RiskLevel


class Effect(StrEnum):
    """What a matching rule does."""

    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

    @property
    def weight(self) -> int:
        return ["allow", "warn", "require_approval", "deny"].index(self.value)


@dataclass
class PolicyContext:
    """Facts about an action being evaluated."""

    action: str
    command: str | None = None
    branch: str | None = None
    target: str | None = None
    profile: str | None = None
    production: bool = False
    host: str | None = None
    """The web site an action acts on: a navigation's destination, else the page in front."""
    app: str | None = None
    """The application a desktop action acts on (named, else the frontmost one)."""


def host_matches(host: str, pattern: str) -> bool:
    """``bank.com`` matches it and its subdomains; a pattern with wildcards is a glob
    (``*.bank.com`` — subdomains only). Never a substring: ``evil.com/?bank.com`` is not bank.com."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(host, pattern)
    return host == pattern or host.endswith("." + pattern)


@dataclass
class PolicyRule:
    """``when`` conditions (all must match) and an ``effect``."""

    id: str
    effect: Effect
    description: str = ""
    message: str = ""
    action: str | None = None
    command: str | None = None
    branch: str | None = None
    target: str | None = None
    profile: str | None = None
    production: bool | None = None
    host: str | None = None
    app: str | None = None
    risk: RiskLevel = RiskLevel.DANGEROUS
    bypassable: bool = True
    _command_re: re.Pattern[str] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.command:
            self._command_re = re.compile(self.command, re.IGNORECASE)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> PolicyRule:
        when = data.get("when") or {}
        return cls(
            id=str(data.get("id") or f"rule-{index}"),
            effect=Effect(data.get("effect", "deny")),
            description=str(data.get("description") or ""),
            message=str(data.get("message") or ""),
            action=when.get("action"),
            command=when.get("command"),
            branch=when.get("branch"),
            target=when.get("target"),
            profile=when.get("profile"),
            production=when.get("production"),
            host=when.get("host"),
            app=when.get("app"),
            risk=RiskLevel.parse(data.get("risk", "dangerous")),
            bypassable=bool(data.get("bypassable", True)),
        )

    def matches(self, ctx: PolicyContext) -> bool:
        checks: list[bool] = []
        if self.action is not None:
            checks.append(fnmatch.fnmatch(ctx.action, self.action))
        if self._command_re is not None:
            checks.append(bool(ctx.command) and bool(self._command_re.search(ctx.command or "")))
        if self.branch is not None:
            checks.append(ctx.branch is not None and fnmatch.fnmatch(ctx.branch, self.branch))
        if self.target is not None:
            checks.append(ctx.target is not None and fnmatch.fnmatch(ctx.target, self.target))
        if self.profile is not None:
            checks.append(ctx.profile is not None and fnmatch.fnmatch(ctx.profile, self.profile))
        if self.production is not None:
            checks.append(ctx.production == self.production)
        if self.host is not None:  # an unknown site never matches: say which actions in `action`
            checks.append(bool(ctx.host) and host_matches(ctx.host or "", self.host))
        if self.app is not None:
            checks.append(bool(ctx.app) and fnmatch.fnmatch((ctx.app or "").lower(), self.app.lower()))
        return bool(checks) and all(checks)

    def text(self) -> str:
        return self.message or self.description or f"policy rule '{self.id}'"
