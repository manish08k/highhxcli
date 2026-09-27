"""HighhX plans and the features they include.

This module is the single source of truth for what *Free* and *Pro* mean. The
CLI uses it to explain the difference and to gate features; the HighhX platform
backend imports the same definitions to enforce them server-side (the backend's
answer always wins — the CLI never unlocks a feature on its own).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FREE = "free"
PRO = "pro"

# Feature identifiers.
CLI = "cli"
"""Every non-AI HighhX command (status, check, test, build, git, security, deploy …)."""
AGENT = "agent"
"""`highhx agent`: the AI developer agent."""
AGENT_CODE_CHANGES = "agent.code_changes"
AGENT_COMMANDS = "agent.commands"
AGENT_GIT = "agent.git"
AGENT_DEPLOY = "agent.deploy"
AGENT_COMPUTER_USE = "agent.computer_use"
"""AI-driven computer use: the agent observes and operates browsers and desktop apps."""
CLOUD_SESSIONS = "cloud.sessions"
"""Agent session history synced to the platform."""


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    tagline: str
    features: frozenset[str]
    highlights: tuple[str, ...]
    monthly_tokens: int = 0
    """AI tokens (input + output) included per billing month through the HighhX gateway."""
    max_steps: int = 0
    """Upper bound for tool-use steps in one agent turn."""
    limits: dict[str, Any] = field(default_factory=dict)

    def includes(self, feature: str) -> bool:
        return feature in self.features

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "features": sorted(self.features),
            "highlights": list(self.highlights),
            "monthly_tokens": self.monthly_tokens,
            "max_steps": self.max_steps,
        }


PLANS: dict[str, Plan] = {
    FREE: Plan(
        id=FREE,
        name="HighhX Free",
        tagline="The powerful developer CLI",
        features=frozenset({CLI}),
        highlights=(
            "The interactive `highhx` session: known requests in plain language run locally, no AI",
            "Deterministic automation — no AI, no account needed",
            "Project detection, init, status, check, test, build, dev, start/stop",
            "Workflows with parallel steps, retries and approvals",
            "git, releases, deployments, rollbacks, services, docker, databases",
            "security scans, doctor, diagnose, repair, logs, history, audit trail",
            "Browser and app automation with known targets (`highhx computer`, `highhx do`)",
        ),
    ),
    PRO: Plan(
        id=PRO,
        name="HighhX Pro",
        tagline="The AI developer agent",
        features=frozenset(
            {
                CLI,
                AGENT,
                AGENT_CODE_CHANGES,
                AGENT_COMMANDS,
                AGENT_GIT,
                AGENT_DEPLOY,
                AGENT_COMPUTER_USE,
                CLOUD_SESSIONS,
            }
        ),
        highlights=(
            "Everything in Free",
            "The AI agent in the same `highhx` session: natural-language tasks, planning, multi-step execution",
            "Recovery and replanning when steps fail",
            "Code changes, debugging, testing, refactoring, git and deployment assistance",
            "AI computer use: operates browsers and desktop apps via accessibility / DOM",
            "Choice of AI provider (Anthropic, OpenAI, Gemini) through the HighhX gateway",
            "Agent sessions synced to your HighhX account",
        ),
        monthly_tokens=20_000_000,
        max_steps=60,
    ),
}


def plan_for(plan_id: str | None) -> Plan:
    """The plan with ``plan_id`` (unknown or missing plans are treated as Free)."""
    return PLANS.get(plan_id or FREE, PLANS[FREE])
