"""Policy evaluation."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.errors import ConfigError
from highhx.policy.rules import Effect, PolicyContext, PolicyRule
from highhx.policy.validator import validate_policies

DEFAULT_PROTECTED_BRANCHES = ("main", "master")


@dataclass
class PolicyDecision:
    """Combined outcome of all matching rules."""

    effect: Effect = Effect.ALLOW
    rules: list[PolicyRule] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.SAFE
    bypassable: bool = True

    @property
    def denied(self) -> bool:
        return self.effect == Effect.DENY

    @property
    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self.rules]


@dataclass
class PolicySet:
    """Parsed policies file."""

    protected_branches: list[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED_BRANCHES))
    require_clean_tree: list[str] = field(default_factory=list)
    forbidden_files: list[str] = field(default_factory=list)
    allowed_release_branches: list[str] = field(default_factory=list)
    rules: list[PolicyRule] = field(default_factory=list)
    source: Path | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, source: Path | None = None) -> PolicySet:
        data = data or {}
        return cls(
            protected_branches=list(data.get("protected_branches") or DEFAULT_PROTECTED_BRANCHES),
            require_clean_tree=list(data.get("require_clean_tree") or []),
            forbidden_files=list(data.get("forbidden_files") or []),
            allowed_release_branches=list(data.get("allowed_release_branches") or []),
            rules=[PolicyRule.from_dict(item, i) for i, item in enumerate(data.get("rules") or [])],
            source=source,
        )

    @classmethod
    def load(cls, path: Path) -> PolicySet:
        """Load and validate ``policies.yaml``; a missing file yields defaults."""
        if not path.exists():
            return cls(source=None)
        from highhx.config.loader import load_yaml

        data = load_yaml(path)
        errors = validate_policies(data)
        if errors:
            raise ConfigError(f"Invalid policies in {path.name}", details=errors, hint="Fix the listed fields.")
        return cls.from_dict(data, source=path)


class PolicyEngine:
    """Evaluates actions against a :class:`PolicySet`."""

    def __init__(self, policies: PolicySet | None = None) -> None:
        self.policies = policies or PolicySet()

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        decision = PolicyDecision()
        for rule in self.policies.rules:
            if not rule.matches(ctx):
                continue
            decision.rules.append(rule)
            decision.messages.append(rule.text())
            if rule.effect.weight > decision.effect.weight:
                decision.effect = rule.effect
            if rule.effect in (Effect.REQUIRE_APPROVAL, Effect.DENY):
                decision.risk = max(decision.risk, rule.risk)
                if not rule.bypassable:
                    decision.bypassable = False
        return decision

    def is_protected_branch(self, branch: str | None) -> bool:
        return branch is not None and any(fnmatch.fnmatch(branch, p) for p in self.policies.protected_branches)

    def requires_clean_tree(self, action: str) -> bool:
        return any(fnmatch.fnmatch(action, p) for p in self.policies.require_clean_tree)

    def release_branch_allowed(self, branch: str | None) -> bool:
        allowed = self.policies.allowed_release_branches
        if not allowed:
            return True
        return branch is not None and any(fnmatch.fnmatch(branch, p) for p in allowed)

    def forbidden_matches(self, paths: Iterable[str]) -> list[str]:
        """Return tracked paths that match ``forbidden_files``."""
        hits: list[str] = []
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in self.policies.forbidden_files):
                hits.append(path)
        return hits
