"""``${{ expression }}`` interpolation in workflow strings."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from highhx.workflows.conditions import EvalContext, ExpressionError, evaluate

INTERPOLATION_RE = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def find_expressions(text: str) -> list[str]:
    return [m.group(1) for m in INTERPOLATION_RE.finditer(text)]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def interpolate(text: str, ctx: EvalContext) -> str:
    """Replace every ``${{ expr }}`` in ``text`` with its evaluated value."""

    def _sub(match: re.Match[str]) -> str:
        try:
            return _stringify(evaluate(match.group(1), ctx))
        except ExpressionError as exc:
            raise ExpressionError(f"in '${{{{ {match.group(1)} }}}}': {exc}") from exc

    return INTERPOLATION_RE.sub(_sub, text)


def interpolate_mapping(values: Mapping[str, Any], ctx: EvalContext) -> dict[str, str]:
    return {key: interpolate(_stringify(value), ctx) for key, value in values.items()}


def try_interpolate(text: str, ctx: EvalContext) -> str:
    """Interpolate, leaving unresolvable expressions untouched (used for dry-run plans)."""

    def _sub(match: re.Match[str]) -> str:
        try:
            return _stringify(evaluate(match.group(1), ctx))
        except ExpressionError:
            return match.group(0)

    return INTERPOLATION_RE.sub(_sub, text)
