"""Git repository access through the engine."""

from __future__ import annotations

from pathlib import Path

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import IntegrationError, NotFoundError, ToolNotFoundError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.git.branches import BRANCH_FORMAT, Branch, parse_branches
from highhx.git.diff import FileChange, parse_numstat
from highhx.git.history import LOG_FORMAT, Commit, parse_log
from highhx.git.status import GitStatus, parse_status_v2
from highhx.git.tags import TAG_FORMAT, Tag, parse_tags
from highhx.utils.processes import which


class GitRepository:
    """Read operations run silently; write operations go through approvals."""

    def __init__(self, engine: Engine, root: Path) -> None:
        self.engine = engine
        self.root = root

    @staticmethod
    def installed() -> bool:
        return which("git") is not None

    def is_repo(self) -> bool:
        if not self.installed():
            return False
        result = self.engine.capture(
            CommandSpec(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.root, timeout=15)
        )
        return result.ok and result.stdout.strip() == "true"

    def require(self) -> None:
        if not self.installed():
            raise ToolNotFoundError("git", purpose="manage the repository")
        if not self.is_repo():
            raise NotFoundError("This directory is not a git repository.", hint="Run `git init` first.")

    def read(self, *args: str, timeout: float = 60, allow_fail: bool = False) -> str:
        result = self.engine.capture(
            CommandSpec(["git", "-c", "core.quotepath=off", *args], cwd=self.root, timeout=timeout)
        )
        if not result.ok and not allow_fail:
            message = (result.stderr.strip().splitlines() or [result.error or "git failed"])[-1]
            raise IntegrationError(f"git {args[0]} failed: {message}")
        return result.stdout if result.ok else ""

    def write(
        self,
        args: list[str],
        *,
        action: str,
        risk: RiskLevel,
        policy_action: str,
        bypassable: bool = True,
        details: list[str] | None = None,
    ) -> CommandResult:
        """Approve (with policy) and run a mutating git command; raises on failure."""
        command = ["git", *args]
        self.engine.approve(
            action,
            risk,
            details=details or [" ".join(command)],
            bypassable=bypassable,
            command=" ".join(command),
            policy_action=policy_action,
        )
        result = self.engine.run(
            CommandSpec(command, cwd=self.root, name=f"git-{args[0]}"), approved=True, policy_action=policy_action
        )
        self.engine.raise_for(result)
        return result

    # ---------------------------------------------------------------- reads
    def status(self) -> GitStatus:
        return parse_status_v2(self.read("status", "--porcelain=v2", "--branch", "-z"))

    def current_branch(self) -> str | None:
        name = self.read("rev-parse", "--abbrev-ref", "HEAD", allow_fail=True).strip()
        return None if not name or name == "HEAD" else name

    def head(self, short: bool = False) -> str | None:
        args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
        value = self.read(*args, allow_fail=True).strip()
        return value or None

    def log(
        self, *, limit: int | None = 20, revision: str | None = None, paths: list[str] | None = None
    ) -> list[Commit]:
        args = ["log", f"--format={LOG_FORMAT}"]
        if limit:
            args.append(f"-n{limit}")
        if revision:
            args.append(revision)
        if paths:
            args += ["--", *paths]
        return parse_log(self.read(*args, allow_fail=True))

    def tags(self) -> list[Tag]:
        return parse_tags(
            self.read("for-each-ref", "refs/tags", "--sort=-creatordate", f"--format={TAG_FORMAT}", allow_fail=True)
        )

    def tag_exists(self, name: str) -> bool:
        return bool(self.read("tag", "--list", name, allow_fail=True).strip())

    def branches(self) -> list[Branch]:
        return parse_branches(
            self.read("for-each-ref", "refs/heads", "--sort=-committerdate", f"--format={BRANCH_FORMAT}")
        )

    def branch_exists(self, name: str) -> bool:
        return self.engine.capture(
            CommandSpec(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=self.root)
        ).ok

    def remotes(self) -> list[str]:
        return [r for r in self.read("remote", allow_fail=True).split() if r]

    def diff_stat(self, *, staged: bool = False, revision: str | None = None) -> list[FileChange]:
        args = ["diff", "--numstat"]
        if staged:
            args.append("--cached")
        if revision:
            args.append(revision)
        return parse_numstat(self.read(*args))

    def diff_text(self, *, staged: bool = False, revision: str | None = None, paths: list[str] | None = None) -> str:
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        if revision:
            args.append(revision)
        if paths:
            args += ["--", *paths]
        return self.read(*args)

    def changed_files(self, base: str | None = None) -> list[str]:
        """Files changed relative to ``base`` (default HEAD) plus untracked files."""
        files: set[str] = set()
        has_head = self.head() is not None
        if base:
            files.update(self.read("diff", "--name-only", f"{base}...HEAD", allow_fail=True).splitlines())
        if has_head:
            files.update(self.read("diff", "--name-only", "HEAD", allow_fail=True).splitlines())
        else:
            files.update(self.read("diff", "--name-only", "--cached", allow_fail=True).splitlines())
        files.update(self.read("ls-files", "--others", "--exclude-standard", allow_fail=True).splitlines())
        return sorted(f for f in files if f)

    def tracked_files(self) -> list[str]:
        return [f for f in self.read("ls-files", "-z").split("\0") if f]

    def is_ignored(self, path: str) -> bool:
        return self.engine.capture(CommandSpec(["git", "check-ignore", "-q", path], cwd=self.root)).ok
