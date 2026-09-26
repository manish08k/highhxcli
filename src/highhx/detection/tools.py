"""Detection of locally installed developer tools and their versions."""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from highhx.utils.processes import which

VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.-]+)?)")

# tool name -> (executable candidates, version args)
KNOWN_TOOLS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "git": (("git",), ("--version",)),
    "python": (("python3", "python", "py"), ("--version",)),
    "pip": (("pip3", "pip"), ("--version",)),
    "uv": (("uv",), ("--version",)),
    "poetry": (("poetry",), ("--version",)),
    "pipenv": (("pipenv",), ("--version",)),
    "pdm": (("pdm",), ("--version",)),
    "node": (("node",), ("--version",)),
    "npm": (("npm",), ("--version",)),
    "pnpm": (("pnpm",), ("--version",)),
    "yarn": (("yarn",), ("--version",)),
    "bun": (("bun",), ("--version",)),
    "deno": (("deno",), ("--version",)),
    "flutter": (("flutter",), ("--version",)),
    "dart": (("dart",), ("--version",)),
    "java": (("java",), ("-version",)),
    "mvn": (("mvn",), ("--version",)),
    "gradle": (("gradle",), ("--version",)),
    "cmake": (("cmake",), ("--version",)),
    "make": (("make", "mingw32-make"), ("--version",)),
    "gcc": (("gcc",), ("--version",)),
    "clang": (("clang",), ("--version",)),
    "go": (("go",), ("version",)),
    "cargo": (("cargo",), ("--version",)),
    "docker": (("docker",), ("--version",)),
    "docker-compose": (("docker-compose",), ("--version",)),
    "kubectl": (("kubectl",), ("version", "--client")),
    "helm": (("helm",), ("version", "--short")),
    "terraform": (("terraform",), ("version",)),
    "ssh": (("ssh",), ("-V",)),
    "psql": (("psql",), ("--version",)),
    "pg_dump": (("pg_dump",), ("--version",)),
    "mysql": (("mysql",), ("--version",)),
    "mysqldump": (("mysqldump",), ("--version",)),
    "sqlite3": (("sqlite3",), ("--version",)),
    "pytest": (("pytest",), ("--version",)),
    "ruff": (("ruff",), ("--version",)),
    "mypy": (("mypy",), ("--version",)),
    "pip-audit": (("pip-audit",), ("--version",)),
    "twine": (("twine",), ("--version",)),
}


@dataclass(frozen=True)
class ToolInfo:
    """Availability and version of a tool."""

    name: str
    available: bool
    path: str | None = None
    version: str | None = None
    raw_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available, "path": self.path, "version": self.version}

    @property
    def major(self) -> int | None:
        if not self.version:
            return None
        head = self.version.split(".", 1)[0]
        return int(head) if head.isdigit() else None


_cache: dict[str, ToolInfo] = {}
_lock = threading.Lock()


def parse_version(text: str) -> str | None:
    """Extract the first version-looking token from tool output."""
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def detect_tool(name: str, *, timeout: float = 8.0, use_cache: bool = True) -> ToolInfo:
    """Locate ``name`` on PATH and query its version (cached per process)."""
    with _lock:
        if use_cache and name in _cache:
            return _cache[name]
    candidates, version_args = KNOWN_TOOLS.get(name, ((name,), ("--version",)))
    path = next((found for c in candidates if (found := which(c))), None)
    if path is None:
        info = ToolInfo(name, False)
    else:
        raw = ""
        returncode = 0
        try:
            proc = subprocess.run(
                [path, *version_args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            raw = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode
        except (OSError, subprocess.TimeoutExpired):
            raw = ""
            returncode = -1
        first = raw.strip().splitlines()[0] if raw.strip() else ""
        version = parse_version(raw)
        # Stubs such as macOS's /usr/bin/java exist on PATH but fail without a runtime.
        works = returncode == 0 or version is not None
        info = ToolInfo(name, works, path if works else None, version if works else None, first or None)
    with _lock:
        _cache[name] = info
    return info


def detect_tools(names: Iterable[str]) -> dict[str, ToolInfo]:
    """Detect several tools in parallel."""
    from highhx.execution.parallel import run_parallel

    unique = list(dict.fromkeys(names))
    outcomes = run_parallel([partial(detect_tool, n) for n in unique], max_workers=8)
    return {name: outcome.value or ToolInfo(name, False) for name, outcome in zip(unique, outcomes, strict=True)}
