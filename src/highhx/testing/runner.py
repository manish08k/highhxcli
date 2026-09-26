"""Test execution and result summarisation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.schema import HighhXConfig
from highhx.core.engine import Engine
from highhx.core.errors import NotFoundError
from highhx.core.result import CommandResult, Status
from highhx.execution.command import CommandSpec, join_command
from highhx.project.detector import ProjectProfile
from highhx.testing.coverage import coverage_command
from highhx.testing.discovery import TestFramework, discover


@dataclass
class TestSummary:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    duration: float | None = None
    parsed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestRun:
    framework: TestFramework
    command: str
    result: CommandResult
    summary: TestSummary
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework.to_dict(),
            "command": self.command,
            "status": str(self.result.status),
            "exit_code": self.result.exit_code,
            "duration": round(self.result.duration, 3),
            "summary": self.summary.to_dict(),
            "notes": self.notes,
        }


def _int(match: re.Match[str] | None, group: int = 1) -> int:
    return int(match.group(group)) if match else 0


def parse_summary(framework: str, output: str) -> TestSummary:
    """Extract counts from common test runner output formats."""
    s = TestSummary()
    text = output[-20000:]
    if framework == "pytest" or re.search(r"=+ .*(passed|failed|error).* in [\d.]+s", text):
        line = next(
            (
                ln
                for ln in reversed(text.splitlines())
                if re.search(r"\b(passed|failed|errors?|no tests ran)\b.* in [\d.]+s", ln)
            ),
            None,
        )
        if line:
            s.passed = _int(re.search(r"(\d+) passed", line))
            s.failed = _int(re.search(r"(\d+) failed", line))
            s.skipped = _int(re.search(r"(\d+) skipped", line))
            s.errors = _int(re.search(r"(\d+) errors?", line))
            d = re.search(r"in ([\d.]+)s", line)
            s.duration = float(d.group(1)) if d else None
            s.parsed = True
    elif framework == "unittest":
        ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
        if ran:
            s.total = int(ran.group(1))
            s.duration = float(ran.group(2))
            s.failed = _int(re.search(r"failures=(\d+)", text))
            s.errors = _int(re.search(r"errors=(\d+)", text))
            s.skipped = _int(re.search(r"skipped=(\d+)", text))
            s.passed = s.total - s.failed - s.errors - s.skipped
            s.parsed = True
    elif framework in ("jest", "vitest", "npm"):
        line = next((ln for ln in reversed(text.splitlines()) if re.match(r"\s*Tests:?\s", ln)), None)
        if line:
            s.passed = _int(re.search(r"(\d+) passed", line))
            s.failed = _int(re.search(r"(\d+) failed", line))
            s.skipped = _int(re.search(r"(\d+) (skipped|todo)", line))
            total = re.search(r"(\d+) total|\((\d+)\)", line)
            s.total = int(total.group(1) or total.group(2)) if total else 0
            s.parsed = True
    elif framework in ("flutter", "dart"):
        match = re.findall(r"\+(\d+)(?: ~(\d+))?(?: -(\d+))?: ", text)
        if match:
            last = match[-1]
            s.passed, s.skipped, s.failed = int(last[0]), int(last[1] or 0), int(last[2] or 0)
            s.parsed = True
    elif framework in ("maven", "gradle"):
        rows = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", text)
        if rows:
            run_total, failures, errors, skipped = (int(x) for x in rows[-1])
            s.total, s.failed, s.errors, s.skipped = run_total, failures, errors, skipped
            s.passed = run_total - failures - errors - skipped
            s.parsed = True
    elif framework == "cargo":
        for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
            s.passed += int(m.group(1))
            s.failed += int(m.group(2))
            s.skipped += int(m.group(3))
            s.parsed = True
    elif framework == "go":
        s.passed = len(re.findall(r"^ok\s", text, re.MULTILINE))
        s.failed = len(re.findall(r"^FAIL\s", text, re.MULTILINE))
        s.parsed = bool(s.passed or s.failed)
    elif framework == "ctest":
        ctest = re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", text)
        if ctest:
            s.total, s.failed = int(ctest.group(3)), int(ctest.group(2))
            s.passed = s.total - s.failed
            s.parsed = True
    if s.parsed and not s.total:
        s.total = s.passed + s.failed + s.skipped + s.errors
    return s


def related_python_tests(root: Path, changed: list[str]) -> list[str]:
    """Test files affected by ``changed`` paths (changed tests + test_<module>.py matches)."""
    selected: list[str] = []
    test_files = [
        p
        for pattern in ("test_*.py", "*_test.py")
        for p in root.rglob(pattern)
        if ".venv" not in p.parts and "node_modules" not in p.parts
    ]
    by_name: dict[str, list[Path]] = {}
    for path in test_files:
        by_name.setdefault(path.name, []).append(path)
    for rel in changed:
        path = Path(rel)
        if path.suffix != ".py":
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            if (root / path).exists():
                selected.append(path.as_posix())
            continue
        for candidate in (f"test_{path.stem}.py", f"{path.stem}_test.py"):
            selected.extend(p.relative_to(root).as_posix() for p in by_name.get(candidate, []))
    return sorted(dict.fromkeys(selected))


def with_args(command: str, args: list[str], framework: str) -> str:
    if not args:
        return command
    extra = join_command(args)
    if framework in ("jest", "vitest", "npm", "mocha") and command.startswith(("npm ", "pnpm ", "bun ")):
        return f"{command} -- {extra}"
    return f"{command} {extra}"


class TestRunner:
    def __init__(self, engine: Engine, root: Path, profile: ProjectProfile, config: HighhXConfig) -> None:
        self.engine = engine
        self.root = root
        self.profile = profile
        self.config = config

    def framework(self) -> TestFramework:
        framework = discover(self.root, self.profile, self.config)
        if framework is None:
            raise NotFoundError(
                "No test command found for this project.",
                hint="Set commands.test in .highhx/config.yaml (e.g. `test: pytest`).",
            )
        return framework

    def build(
        self, *, coverage: bool = False, changed: list[str] | None = None, args: list[str] | None = None
    ) -> tuple[TestFramework, str | None, list[str]]:
        """Return (framework, command-or-None-if-nothing-to-run, notes)."""
        framework = self.framework()
        command = framework.command
        notes: list[str] = []
        extra = list(args or [])
        if changed is not None:
            if framework.name == "pytest":
                tests = related_python_tests(self.root, changed)
                if not tests:
                    return framework, None, ["No tests are related to the changed files."]
                extra = [*tests, *extra]
                notes.append(f"Running {len(tests)} test file(s) related to {len(changed)} changed file(s).")
            elif framework.name == "jest":
                extra.append("--onlyChanged")
            elif framework.name == "vitest":
                extra.append("--changed")
            else:
                notes.append(f"{framework.name} cannot select tests by changed files; running the full suite.")
        if coverage:
            configured = self.config.commands.get("coverage")
            command = configured or coverage_command(framework.name, command, self._dependencies())
        return framework, with_args(command, extra, framework.name), notes

    def _dependencies(self) -> set[str]:
        deps: set[str] = set()
        for manifest in self.profile.manifests:
            deps |= manifest.dependencies
        return deps

    def run(
        self, *, coverage: bool = False, changed: list[str] | None = None, args: list[str] | None = None
    ) -> TestRun:
        framework, command, notes = self.build(coverage=coverage, changed=changed, args=args)
        if command is None:
            skipped = CommandResult("(no tests selected)", 0, Status.SKIPPED)
            return TestRun(framework, "", skipped, TestSummary(parsed=True), notes)
        output: list[str] = []
        result = self.engine.run(
            CommandSpec(command, cwd=self.root, name="test"),
            action=f"Run tests: {command}",
            risk=RiskLevel.NORMAL,
            policy_action="test",
            on_line=lambda _stream, line: output.append(line),
        )
        summary = parse_summary(framework.name, "\n".join(output))
        return TestRun(framework, command, result, summary, notes)
