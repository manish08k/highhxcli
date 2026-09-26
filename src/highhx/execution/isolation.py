"""Environment isolation for less-trusted commands (e.g. plugin commands).

This is *not* a security sandbox: it limits which environment variables leak
into a child process and gives it a private scratch directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

SAFE_ENV_VARS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SHELL",
        "SYSTEMROOT",
        "SystemRoot",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMFILES",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "NO_COLOR",
        "FORCE_COLOR",
        "CI",
    }
)


def isolated_environment(
    allow: Iterable[str] = (),
    *,
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment containing only safe variables plus ``allow``-listed names and ``extra``."""
    source = base if base is not None else os.environ
    allowed = SAFE_ENV_VARS | set(allow)
    env = {k: v for k, v in source.items() if k in allowed}
    env.update(extra or {})
    return env
