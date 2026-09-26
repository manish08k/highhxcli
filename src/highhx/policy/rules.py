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
        return bool(checks) and all(checks)

    def text(self) -> str:
        return self.message or self.description or f"policy rule '{self.id}'"
