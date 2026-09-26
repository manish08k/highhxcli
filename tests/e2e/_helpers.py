"""Run the real CLI entry point in a subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Proc:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def highhx(*args: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 120) -> Proc:
    completed = subprocess.run(
        [sys.executable, "-m", "highhx", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return Proc(completed.returncode, completed.stdout, completed.stderr)
