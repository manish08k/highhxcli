"""Schema for ``policies.yaml``."""

from __future__ import annotations

from typing import Any

from highhx.approvals.risk import RISK_NAMES
from highhx.utils.validation import Bool, Int, List, Obj, Prop, Str, is_identifier

EFFECTS = ("allow", "warn", "require_approval", "deny")


def _valid_regex(value: str) -> str | None:
    import re

    try:
        re.compile(value)
    except re.error as exc:
        return f"invalid regular expression: {exc}"
    return None


WHEN_SCHEMA = Obj(
    {
        "action": Prop(Str(), description="Glob matched against the action name, e.g. 'deploy:*'"),
        "command": Prop(Str(check=_valid_regex), description="Regex matched against the command line"),
        "branch": Prop(Str(), description="Glob matched against the current git branch"),
        "target": Prop(Str(), description="Glob matched against the deployment target"),
        "profile": Prop(Str(), description="Glob matched against the environment profile"),
        "production": Prop(Bool()),
    }
)

RULE_SCHEMA = Obj(
    {
        "id": Prop(Str(check=lambda v: None if is_identifier(v) else "must be an identifier"), required=True),
        "description": Prop(Str()),
        "message": Prop(Str()),
        "when": Prop(WHEN_SCHEMA, required=True),
        "effect": Prop(Str(choices=EFFECTS), required=True),
        "risk": Prop(Str(choices=RISK_NAMES)),
        "bypassable": Prop(Bool()),
    },
    check=lambda rule: [] if rule.get("when") else ["'when' must contain at least one condition"],
)

POLICY_SCHEMA = Obj(
    {
        "version": Prop(Int(minimum=1, maximum=1)),
        "protected_branches": Prop(List(Str())),
        "require_clean_tree": Prop(List(Str())),
        "forbidden_files": Prop(List(Str()), description="Globs that must never be committed"),
        "allowed_release_branches": Prop(List(Str())),
        "rules": Prop(List(RULE_SCHEMA)),
    }
)


def validate_policies(data: Any) -> list[str]:
    """Return a list of problems with a parsed policies document."""
    if data is None:
        return []
    errors = POLICY_SCHEMA.validate(data, "")
    if not errors:
        ids = [rule["id"] for rule in data.get("rules") or []]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        errors.extend(f"rules: duplicate rule id '{d}'" for d in dupes)
    return errors
