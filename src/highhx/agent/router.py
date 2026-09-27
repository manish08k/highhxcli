"""Routing a plain-language request when the AI agent is not available (HighhX Free).

Requests the deterministic resolver understands completely (:mod:`highhx.actions.resolver`)
run as actions. Anything else needs understanding — the AI agent's job (HighhX Pro). For
those the router names the capability the request needs and the actions that can do part
of it locally, so the user can continue without the agent. No model is involved: this is
keyword matching over the request, and it only chooses what to *suggest*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.actions.resolver import Resolution, ResolverContext
from highhx.cloud.capabilities import Capability
from highhx.language.grammar import Unknown

if TYPE_CHECKING:
    from highhx.decision.deterministic import Decision

PRO_LEAD = "HighhX Pro can understand and execute this open-ended task."


@dataclass(frozen=True)
class LocalAction:
    """An action offered as a local alternative."""

    label: str
    action: str
    inputs: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def command(self) -> str:
        return self.action


@dataclass(frozen=True)
class Route:
    resolution: Resolution | None = None
    """Set when the request resolves deterministically."""
    capability: Capability | None = None
    """Set when the request needs a capability the session does not have."""
    reason: str = ""
    alternatives: tuple[LocalAction, ...] = field(default_factory=tuple)
    unknown: Unknown | None = None
    """Set when HighhX recognised the action but not an entity ("open spotifyy")."""
    decision: Decision | None = field(default=None, compare=False)
    """The deterministic decision (plan, entities, risk) for tracing and previews."""


# (pattern, capability, what needs Pro). First match wins; most specific first.
_CAPABILITIES: tuple[tuple[str, Capability, str], ...] = (
    (
        r"\b(browser|web ?page|website|web ?site|click|fill (?:in|out)|form|screenshot|sign in to|log ?in to|"
        r"desktop app|safari|chrome|firefox|scrape)\b",
        Capability.AI_COMPUTER_USE,
        "AI browser and desktop automation requires HighhX Pro.",
    ),
    (
        r"\b(why|debug\w*|failing|fails|failed|broken|crash\w*|bug|bugs|error|errors|exception|500|traceback)\b",
        Capability.AI_AGENT,
        "AI debugging requires HighhX Pro.",
    ),
    (
        r"\b(deploy\w*|ship|roll ?out|release to|go live|rollback|roll back)\b",
        Capability.AI_DEPLOY,
        "AI-driven deployment requires HighhX Pro.",
    ),
    (
        r"\b(commit|push|merge|rebase|cherry-pick|open a pr|pull request|branch)\b",
        Capability.AI_GIT,
        "AI git operations require HighhX Pro.",
    ),
    (
        r"\b(fix\w*|refactor\w*|implement\w*|add|adds|adding|build|create|write|update|upgrade|change|"
        r"edit|rename|migrate|convert|generate|remove|delete|optimi[sz]e|improve|make)\b",
        Capability.AI_CODE_CHANGES,
        "AI code changes require HighhX Pro.",
    ),
)

# Keyword → actions that do part of the job deterministically.
_ALTERNATIVES: tuple[tuple[str, tuple[LocalAction, ...]], ...] = (
    (r"\b(tests?|failing|pytest|jest|specs?)\b", (LocalAction("Run the tests", "project.test"),)),
    (r"\b(lint\w*|type ?check\w*|checks?|quality)\b", (LocalAction("Run the checks", "project.check"),)),
    (r"\b(format\w*|lint\w*|style)\b", (LocalAction("Apply formatter and linter fixes", "project.fix"),)),
    (
        r"\b(build|compile|bundle|package)\b(?!\s+(?:a|an|me|new)\b)",
        (LocalAction("Build the project", "project.build"),),
    ),
    (
        r"\b(deploy\w*|ship|release|go live|production|rollback|roll back)\b",
        (
            LocalAction("Deploy with your configured targets", "deployment.deploy"),
            LocalAction("Deployment status", "deployment.status"),
        ),
    ),
    (r"\b(security|vulnerab\w*|secrets?|cve|audit)\b", (LocalAction("Run a security scan", "security.scan"),)),
    (
        r"\b(dependenc\w*|packages?|deps|install|upgrade)\b",
        (LocalAction("List outdated dependencies", "package.outdated"),),
    ),
    (
        r"\b(git|commit|push|diff|branch|changes|merge)\b",
        (LocalAction("Show git status", "git.status"), LocalAction("Show the diff", "git.diff")),
    ),
    (
        r"\b(error|errors|crash\w*|500|exception|bug|bugs|broken|why|failing|slow|debug\w*)\b",
        (LocalAction("Diagnose the project", "security.diagnose"), LocalAction("Show recent logs", "service.logs")),
    ),
    (r"\b(environment|setup|install\w*|toolchain|doctor)\b", (LocalAction("Check your setup", "security.doctor"),)),
    (
        r"\b(repo\w*|project|codebase|explain|overview|structure|architecture|understand|what)\b",
        (LocalAction("Project details", "project.detect"), LocalAction("Project status", "project.status")),
    ),
)
MAX_ALTERNATIVES = 3


def required_capability(text: str) -> tuple[Capability, str]:
    """The capability an open-ended request needs, and a one-line explanation."""
    low = text.lower()
    for pattern, capability, reason in _CAPABILITIES:
        if re.search(pattern, low):
            return capability, reason
    return Capability.AI_AGENT, "Understanding and multi-step requests use the AI agent, part of HighhX Pro."


def local_alternatives(text: str) -> tuple[LocalAction, ...]:
    """Actions that can do (part of) ``text`` locally, most relevant first."""
    low = text.lower()
    found: list[LocalAction] = []
    for pattern, actions in _ALTERNATIVES:
        if re.search(pattern, low):
            found += [a for a in actions if a not in found]
    return tuple(found[:MAX_ALTERNATIVES])


def route(text: str, context: ResolverContext | None = None, *, decision: Decision | None = None) -> Route:
    """How a session without the AI agent handles ``text``: the deterministic plan, or the capability it needs."""
    from highhx.decision.deterministic import DeterministicDecider

    decision = decision or DeterministicDecider(context).decide(text)
    if decision.resolution is not None:
        return Route(resolution=decision.resolution, decision=decision)
    if decision.unknown is not None:
        return Route(
            capability=Capability.AI_AGENT,
            reason=f"{decision.unknown.reason} Open-ended requests are for HighhX Pro.",
            unknown=decision.unknown,
            decision=decision,
        )
    capability, reason = required_capability(text)
    return Route(capability=capability, reason=reason, alternatives=local_alternatives(text), decision=decision)
