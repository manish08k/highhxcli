"""Routing a plain-language request when the AI agent is not available (HighhX Free).

Requests that map to a known action (``run the tests``, ``show git status``,
``open localhost:3000``) are carried out deterministically by
:mod:`highhx.computer.intents` — the same rules as ``highhx do``. Anything else
needs understanding, which is the AI agent's job (HighhX Pro). For those the
router names the capability the request needs and the real HighhX commands that
can do part of it locally, so the user can continue without the agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from highhx.cloud.capabilities import Capability
from highhx.computer.intents import Intent, parse


@dataclass(frozen=True)
class LocalAction:
    """A HighhX command offered as a local alternative."""

    label: str
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return "highhx " + " ".join(self.argv)


@dataclass(frozen=True)
class Route:
    intent: Intent | None = None
    """Set when the request is carried out deterministically."""
    capability: Capability | None = None
    """Set when the request needs a capability the session does not have."""
    reason: str = ""
    alternatives: tuple[LocalAction, ...] = field(default_factory=tuple)


# (pattern, capability, what needs Pro). First match wins; order is most specific first.
_CAPABILITIES: tuple[tuple[str, Capability, str], ...] = (
    (
        r"\b(browser|web ?page|website|web ?site|click|fill (?:in|out)|form|screenshot|sign in to|log ?in to|"
        r"desktop app|safari|chrome|firefox|scrape)\b",
        Capability.AI_COMPUTER_USE,
        "AI browser and desktop automation requires HighhX Pro.",
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

# Keyword → local HighhX commands that do part of the job deterministically.
_ALTERNATIVES: tuple[tuple[str, tuple[LocalAction, ...]], ...] = (
    (r"\b(tests?|failing|pytest|jest|specs?)\b", (LocalAction("Run the tests", ("test",)),)),
    (r"\b(lint\w*|type ?check\w*|checks?|quality)\b", (LocalAction("Run the checks", ("check",)),)),
    (r"\b(format\w*|lint\w*|style)\b", (LocalAction("Apply automatic fixes (formatters, linters)", ("fix",)),)),
    (r"\b(build|compile|bundle|package)\b(?!\s+(?:a|an|me|new)\b)", (LocalAction("Build the project", ("build",)),)),
    (
        r"\b(deploy\w*|ship|release|go live|production|rollback|roll back)\b",
        (LocalAction("Deploy with your configured targets", ("deploy",)),),
    ),
    (r"\b(security|vulnerab\w*|secrets?|cve|audit)\b", (LocalAction("Run a security scan", ("security",)),)),
    (
        r"\b(dependenc\w*|packages?|deps|install|upgrade)\b",
        (LocalAction("List outdated dependencies", ("deps", "outdated")),),
    ),
    (
        r"\b(git|commit|push|diff|branch|changes|merge)\b",
        (LocalAction("Show git status", ("git", "status")), LocalAction("Show the diff", ("git", "diff"))),
    ),
    (
        r"\b(error|errors|crash\w*|500|exception|bug|broken|why|failing|slow)\b",
        (LocalAction("Diagnose the project", ("diagnose",)), LocalAction("Show recent logs", ("logs",))),
    ),
    (
        r"\b(browser|web ?page|website|web ?site|click|form|screenshot|desktop app|safari|chrome|firefox)\b",
        (LocalAction("What deterministic browser/app automation can run here", ("computer", "status")),),
    ),
    (r"\b(environment|setup|install\w*|toolchain|doctor)\b", (LocalAction("Check your setup", ("doctor",)),)),
    (
        r"\b(repo\w*|project|codebase|explain|overview|structure|architecture|understand|what)\b",
        (LocalAction("Project information", ("info",)), LocalAction("Project status", ("status",))),
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
    """HighhX commands that can do (part of) ``text`` locally, most relevant first."""
    low = text.lower()
    found: list[LocalAction] = []
    for pattern, actions in _ALTERNATIVES:
        if re.search(pattern, low):
            found += [a for a in actions if a not in found]
    return tuple(found[:MAX_ALTERNATIVES])


def route(text: str) -> Route:
    """How a Free session handles ``text``: a deterministic intent, or the capability it needs."""
    intent = parse(text)
    if intent is not None:
        return Route(intent=intent)
    capability, reason = required_capability(text)
    return Route(capability=capability, reason=reason, alternatives=local_alternatives(text))
