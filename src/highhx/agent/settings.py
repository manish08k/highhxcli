"""Effective agent settings: command-line flags > project config (``agent:`` in
``.highhx/config.yaml``) > account settings on the HighhX platform > defaults."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from highhx.agent.permissions import ApprovalMode

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_STEPS = 60


@dataclass
class AgentSettings:
    provider: str = "highhx"
    model: str | None = None
    approval: ApprovalMode = ApprovalMode.ASK
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = 32_000
    effort: str | None = None
    instructions: str = ""
    sync_sessions: bool = True

    @classmethod
    def resolve(
        cls,
        *,
        project: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        plan_max_steps: int = 0,
    ) -> AgentSettings:
        merged: dict[str, Any] = {}
        for layer in (account or {}, project or {}, overrides or {}):
            merged.update({k: v for k, v in layer.items() if v is not None})
        settings = cls()
        if merged.get("provider"):
            settings.provider = str(merged["provider"])
        if merged.get("model"):
            settings.model = str(merged["model"])
        if merged.get("approval"):
            settings.approval = ApprovalMode(str(merged["approval"]))
        if merged.get("max_steps"):
            settings.max_steps = int(merged["max_steps"])
        if merged.get("max_tokens"):
            settings.max_tokens = int(merged["max_tokens"])
        if merged.get("effort") in EFFORTS:
            settings.effort = str(merged["effort"])
        if merged.get("instructions"):
            settings.instructions = str(merged["instructions"])
        if "sync_sessions" in merged:
            settings.sync_sessions = bool(merged["sync_sessions"])
        if plan_max_steps:
            settings.max_steps = min(settings.max_steps, plan_max_steps)
        return settings
