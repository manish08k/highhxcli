"""Approval policy: which risk levels need confirmation and which may be bypassed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.approvals.rules import RiskRule


@dataclass
class ApprovalPolicy:
    """Approval behaviour.

    auto_approve        actions at or below this risk never prompt
    yes_max_risk        highest risk ``--yes`` may approve non-interactively
    typed_confirmation  risk at which the user must type ``yes`` instead of y/N
    non_bypassable      rule ids / action names that always need interactive approval
    rules               extra command risk rules from configuration
    """

    auto_approve: RiskLevel = RiskLevel.NORMAL
    yes_max_risk: RiskLevel = RiskLevel.CRITICAL
    typed_confirmation: RiskLevel = RiskLevel.CRITICAL
    non_bypassable: list[str] = field(default_factory=list)
    rules: list[RiskRule] = field(default_factory=list)

    @classmethod
    def from_config(cls, data: Mapping[str, Any] | None) -> ApprovalPolicy:
        data = data or {}
        rules = [
            RiskRule(
                id=str(item.get("id") or f"custom-{index}"),
                pattern=str(item["pattern"]),
                risk=RiskLevel.parse(item.get("risk", "dangerous")),
                reason=str(item.get("reason") or "matches a project approval rule"),
                bypassable=bool(item.get("bypassable", True)),
            )
            for index, item in enumerate(data.get("rules") or [])
        ]
        return cls(
            auto_approve=RiskLevel.parse(data.get("auto_approve", "normal")),
            yes_max_risk=RiskLevel.parse(data.get("yes_max_risk", "critical")),
            typed_confirmation=RiskLevel.parse(data.get("typed_confirmation", "critical")),
            non_bypassable=[str(x) for x in data.get("non_bypassable") or []],
            rules=rules,
        )

    def is_non_bypassable(self, identifiers: list[str]) -> bool:
        return any(identifier in self.non_bypassable for identifier in identifiers)
