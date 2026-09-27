"""Risk and approval: one table, decided by HighhX — never by a model.

Actions are rated on five levels. The deterministic safety classifier
(:mod:`highhx.safety.classifier`) rates the concrete action from its structure (the
command, the path, the environment …); the action catalog sets a floor for what the
wording cannot show (a database migration is HIGH whatever the command looks like).
The effective risk is the higher of the two, plus a few action-level rules.

=========  ==============================================  ===========================
risk       examples                                        approval
=========  ==============================================  ===========================
SAFE       git status, git diff, read a file, status       never asked
LOW        run tests, build, lint                          user: no · agent: per mode
MEDIUM     write a file, git commit, install packages      always asked (user --yes ok)
HIGH       git push, deploy, database migration, delete    always asked (user --yes ok)
CRITICAL   production deploy, drop database, rm -rf        typed confirmation, no --yes
=========  ==============================================  ===========================

Blocked actions (e.g. deleting the filesystem root) never run through automation.
The AI agent can never pre-approve anything; `--yes` covers only a user's own
deterministic actions below CRITICAL. The confirmation itself, its binding to the
exact action and the audit trail are the existing :class:`~highhx.safety.gate.ActionGate`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import IntEnum

from highhx.approvals.risk import RiskLevel
from highhx.safety.actions import Actor
from highhx.safety.classifier import SafetyVerdict


class Risk(IntEnum):
    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def level(self) -> RiskLevel:
        """The engine's four-level scale (used by the gate, policies and approvals)."""
        return {
            Risk.SAFE: RiskLevel.SAFE,
            Risk.LOW: RiskLevel.NORMAL,
            Risk.MEDIUM: RiskLevel.NORMAL,
            Risk.HIGH: RiskLevel.DANGEROUS,
            Risk.CRITICAL: RiskLevel.CRITICAL,
        }[self]

    @classmethod
    def parse(cls, value: str | int | Risk) -> Risk:
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(value)
        name = str(value).strip().upper()
        aliases = {"NORMAL": "LOW", "DANGEROUS": "HIGH"}
        try:
            return cls[aliases.get(name, name)]
        except KeyError:
            valid = ", ".join(r.label for r in cls)
            raise ValueError(f"unknown risk {value!r} (expected one of: {valid})") from None


def from_verdict(verdict: SafetyVerdict) -> Risk:
    """The classifier's verdict on the five-level scale."""
    if verdict.risk >= RiskLevel.CRITICAL:
        return Risk.CRITICAL
    if verdict.risk >= RiskLevel.DANGEROUS:
        return Risk.HIGH
    if verdict.requires_confirmation:
        return Risk.MEDIUM  # sensitive categories (installs, credentials …) at normal risk
    if verdict.risk >= RiskLevel.NORMAL:
        return Risk.LOW
    return Risk.SAFE


class Approval(IntEnum):
    NONE = 0
    """Runs without asking."""
    MODE = 1
    """The agent's approval mode decides (ask / auto-edit); a user's own action runs."""
    ASK = 2
    """Always asked; a user's own action may be pre-approved with --yes."""
    TYPED = 3
    """Typed confirmation in an interactive terminal; nothing pre-approves it."""


APPROVAL_RULES: dict[Risk, Approval] = {
    Risk.SAFE: Approval.NONE,
    Risk.LOW: Approval.MODE,
    Risk.MEDIUM: Approval.ASK,
    Risk.HIGH: Approval.ASK,
    Risk.CRITICAL: Approval.TYPED,
}
"""The single approval table. Every action — Free or Pro, command, workflow step or agent
proposal — is approved according to its effective risk here."""


@dataclass(frozen=True)
class Decision:
    risk: Risk
    approval: Approval
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blocked: bool = False

    @property
    def asks(self) -> bool:
        return self.approval >= Approval.ASK

    def asks_for(self, actor: Actor) -> bool:
        """Whether a person is asked for this actor (MODE: the agent asks, the user's own does not)."""
        if self.approval == Approval.MODE:
            return actor == Actor.AGENT
        return self.asks


_RM = re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)rm\s", re.I)


def _recursive_force_delete(command: str) -> bool:
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            argv = shlex.split(segment)
        except ValueError:
            argv = segment.split()
        if argv[:1] == ["sudo"]:
            argv = argv[1:]
        if not argv or argv[0] != "rm":
            continue
        flags = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
        long = {a for a in argv[1:] if a.startswith("--")}
        recursive = "r" in flags.lower() or "--recursive" in long
        force = "f" in flags or "--force" in long
        if recursive and force:
            return True
    return False


def extra_rules(command: str | None) -> tuple[Risk, str] | None:
    """Action-level rules on top of the classifier."""
    if command and _RM.search(command) and _recursive_force_delete(command):
        return Risk.CRITICAL, "recursive forced delete (rm -rf)"
    return None


def decide(base: Risk, verdict: SafetyVerdict, *, command: str | None = None) -> Decision:
    """The effective risk and approval for an action: the catalog floor, the classifier's
    verdict and the action-level rules — the highest wins."""
    risk = max(base, from_verdict(verdict))
    reasons = list(verdict.reasons)
    rule = extra_rules(command)
    if rule is not None and rule[0] > risk:
        risk = rule[0]
        reasons.append(rule[1])
    return Decision(risk, APPROVAL_RULES[risk], tuple(dict.fromkeys(reasons)), verdict.blocked)
