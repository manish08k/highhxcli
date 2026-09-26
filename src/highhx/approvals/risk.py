"""Risk levels and command risk classification."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum


class RiskLevel(IntEnum):
    """How dangerous an action is.

    SAFE       read-only / no side effects (read files, status)
    NORMAL     local, reversible side effects (run tests, install packages)
    DANGEROUS  shared or hard-to-reverse effects (git push, delete build output)
    CRITICAL   production or data-destroying effects (prod deploy, drop database)
    """

    SAFE = 0
    NORMAL = 1
    DANGEROUS = 2
    CRITICAL = 3

    @classmethod
    def parse(cls, value: str | int | RiskLevel) -> RiskLevel:
        if isinstance(value, RiskLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            valid = ", ".join(level.name.lower() for level in cls)
            raise ValueError(f"unknown risk level {value!r} (expected one of: {valid})") from None

    @property
    def label(self) -> str:
        return self.name.lower()


RISK_NAMES = tuple(level.label for level in RiskLevel)


@dataclass
class Classification:
    """Result of classifying a command."""

    risk: RiskLevel
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    bypassable: bool = True


def classify_command(command: str, rules: Iterable[object] | None = None) -> Classification:
    """Classify ``command`` using the built-in rules plus any extra ``rules``.

    The highest matching risk wins; a command matching no rule is NORMAL.
    """
    from highhx.approvals.rules import BUILTIN_RULES, RiskRule

    candidates: list[RiskRule] = list(BUILTIN_RULES)
    candidates.extend(r for r in (rules or ()) if isinstance(r, RiskRule))
    result = Classification(RiskLevel.NORMAL)
    matched = False
    for rule in candidates:
        if rule.matches(command):
            result.risk = max(result.risk, rule.risk) if matched else rule.risk
            matched = True
            result.reasons.append(rule.reason)
            result.rule_ids.append(rule.id)
            if not rule.bypassable:
                result.bypassable = False
    return result
