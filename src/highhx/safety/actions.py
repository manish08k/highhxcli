"""What an action *is*: a structured, canonical description used for classification,
confirmation, execution binding and audit. Free (deterministic) and Pro (agent)
actions are described the same way and go through the same checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    READ = "read"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    EXEC = "exec"
    GIT = "git"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    UI_CLICK = "ui_click"
    UI_TYPE = "ui_type"
    UI_KEY = "ui_key"
    UI_SELECT = "ui_select"
    UI_SCROLL = "ui_scroll"
    UI_UPLOAD = "ui_upload"
    """Choose local files in a page's file field (their contents can then leave the machine)."""
    NAVIGATE = "navigate"
    APP_LAUNCH = "app_launch"


class Actor(StrEnum):
    USER = "user"
    """A deterministic action the user specified explicitly (CLI command, flow file)."""
    AGENT = "agent"
    """An action proposed by the AI agent."""


@dataclass(frozen=True)
class ActionDescriptor:
    """A single concrete action, described precisely enough to classify, show and bind."""

    kind: ActionKind
    summary: str
    """Exact human-readable action, e.g. ``Click button "Delete account"``."""
    tool: str
    target: str = ""
    """The affected resource: a path, element, URL, deploy target, database …"""
    application: str = ""
    """Application / system the action happens in (project, Chrome, PostgreSQL …)."""
    command: str | None = None
    environment: str | None = None
    """e.g. ``production`` when known."""
    actor: Actor = Actor.AGENT
    attributes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Structural facts used by the classifier (element role/type, form method, href …)."""

    def attr(self, name: str, default: str = "") -> str:
        for key, value in self.attributes:
            if key == name:
                return value
        return default

    def canonical(self) -> str:
        data: dict[str, Any] = asdict(self)
        data["attributes"] = sorted(self.attributes)
        return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def attrs(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in values.items() if v not in (None, "")))
