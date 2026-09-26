"""Deployment target helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping

from highhx.config.schema import DeployTargetConfig

TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/:@-]*$")


def is_safe_value(value: str) -> bool:
    """Values substituted into deploy commands may not contain shell metacharacters."""
    return bool(SAFE_VALUE_RE.match(value)) and ".." not in value


def render_command(command: str, values: Mapping[str, str | None]) -> str:
    """Substitute ``{{ version }}``, ``{{ previous_version }}``, ``{{ git_sha }}``, ``{{ target }}``…

    Substituted values are restricted to a safe character set so that a version
    label can never inject shell syntax into the deployment command.
    """

    def _sub(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if value is None:
            raise ValueError(f"'{{{{ {match.group(1)} }}}}' has no value for this deployment")
        text = str(value)
        if not is_safe_value(text):
            raise ValueError(
                f"unsafe value {text!r} for '{{{{ {match.group(1)} }}}}' (allowed: letters, digits and . _ + / : @ -)"
            )
        return text

    return TEMPLATE_RE.sub(_sub, command)


def describe(target: DeployTargetConfig) -> dict[str, object]:
    where = target.host or target.context or target.compose_file or target.directory or target.command or ""
    return {
        "name": target.name,
        "type": target.type,
        "production": target.production,
        "description": target.description,
        "where": where,
        "health_check": bool(target.health_check),
        "rollback": bool(target.rollback_command) or target.type in ("kubernetes", "docker"),
    }
