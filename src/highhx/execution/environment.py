"""Process environment construction."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping


def build_environment(
    extra: Mapping[str, object] | None = None,
    *,
    base: Mapping[str, str] | None = None,
    inherit: bool = True,
    remove: Iterable[str] = (),
) -> dict[str, str]:
    """Merge ``extra`` over the base environment.

    Values are converted to strings; ``None`` values remove the variable.
    """
    env: dict[str, str] = dict(base if base is not None else os.environ) if inherit else {}
    for key in remove:
        env.pop(key, None)
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        elif isinstance(value, bool):
            env[key] = "true" if value else "false"
        else:
            env[key] = str(value)
    return env
