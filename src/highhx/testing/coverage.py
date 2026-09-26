"""Coverage flags per framework."""

from __future__ import annotations

from highhx.core.errors import UsageError
from highhx.utils.processes import which


def coverage_command(framework: str, command: str, dependencies: set[str]) -> str:
    """Return the command with coverage enabled, or raise with guidance."""
    if framework == "pytest":
        if "pytest-cov" in dependencies:
            return f"{command} --cov --cov-report=term-missing"
        if "coverage" in dependencies or which("coverage"):
            runner = command.split("pytest", 1)[0]
            prefix = (
                runner.replace("python3 -m ", "").replace("python -m ", "")
                if runner.strip().startswith("python")
                else runner
            )
            return f"{prefix}coverage run -m pytest && {prefix}coverage report"
        raise UsageError(
            "Coverage for pytest needs pytest-cov or coverage.", hint="Add pytest-cov to your dev dependencies."
        )
    if framework == "unittest":
        if "coverage" in dependencies or which("coverage"):
            return "coverage run -m unittest discover && coverage report"
        raise UsageError("Coverage for unittest needs the 'coverage' package.", hint="pip install coverage")
    if framework in ("jest", "vitest"):
        return _append(command, "--coverage", npm_style=not command.startswith(("npx", framework)))
    if framework in ("flutter", "dart"):
        return f"{command} --coverage" if framework == "flutter" else f"{command} --coverage=coverage"
    if framework == "go":
        return f"{command} -cover"
    raise UsageError(
        f"HighhX does not know how to enable coverage for '{framework}'.",
        hint="Configure commands.coverage in .highhx/config.yaml.",
    )


def _append(command: str, flag: str, *, npm_style: bool) -> str:
    if npm_style and any(command.startswith(p) for p in ("npm ", "pnpm ", "yarn ", "bun ")):
        separator = "" if command.startswith("yarn ") else " --"
        return f"{command}{separator} {flag}"
    return f"{command} {flag}"
