"""High-level Git workflows with safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import IntegrationError, UsageError, ValidationError
from highhx.git.commits import validate_message
from highhx.git.repository import GitRepository
from highhx.git.status import GitStatus
from highhx.policy.engine import PolicyEngine


@dataclass
class SyncOutcome:
    fetched: bool
    pulled: bool
    pushed: bool
    ahead: int
    behind: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class GitManager:
    def __init__(self, engine: Engine, repo: GitRepository, policy: PolicyEngine | None = None) -> None:
        self.engine = engine
        self.repo = repo
        self.policy = policy or PolicyEngine()

    def status(self) -> GitStatus:
        self.repo.require()
        return self.repo.status()

    # ------------------------------------------------------------ branches
    def create_branch(self, name: str, *, start: str | None = None, switch: bool = True) -> None:
        self.repo.require()
        if self.repo.branch_exists(name):
            raise ValidationError(
                f"Branch '{name}' already exists.", hint=f"Switch to it with `highhx git branch --switch {name}`."
            )
        args = ["switch", "-c", name] if switch else ["branch", name]
        if start:
            args.append(start)
        self.repo.write(args, action=f"Create branch '{name}'", risk=RiskLevel.NORMAL, policy_action="git:branch")

    def switch(self, name: str) -> None:
        self.repo.require()
        status = self.repo.status()
        if status.conflicted:
            raise ValidationError("Resolve merge conflicts before switching branches.")
        self.repo.write(
            ["switch", name], action=f"Switch to branch '{name}'", risk=RiskLevel.NORMAL, policy_action="git:switch"
        )

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        self.repo.require()
        if name == self.repo.current_branch():
            raise UsageError(f"Cannot delete the current branch '{name}'.", hint="Switch to another branch first.")
        protected = self.policy.is_protected_branch(name)
        risk = RiskLevel.CRITICAL if (force or protected) else RiskLevel.DANGEROUS
        self.repo.write(
            ["branch", "-D" if force else "-d", name],
            action=f"{'Force-delete' if force else 'Delete'} branch '{name}'" + (" (protected)" if protected else ""),
            risk=risk,
            policy_action="git:branch-delete",
            bypassable=not protected,
        )

    # ------------------------------------------------------------- commits
    def commit(
        self,
        message: str,
        *,
        all_changes: bool = False,
        paths: list[str] | None = None,
        conventional: bool = False,
        allow_empty: bool = False,
    ) -> str | None:
        self.repo.require()
        problems = validate_message(message, conventional=conventional)
        blocking = [p for p in problems if "empty" in p or (conventional and "Conventional" in p)]
        if blocking:
            raise ValidationError("Invalid commit message", details=problems)
        for problem in problems:
            self.engine.output.warn(f"commit message: {problem}")
        status = self.repo.status()
        if status.conflicted:
            raise ValidationError("Cannot commit with unresolved conflicts.", details=status.conflicted)
        if paths:
            self.repo.write(
                ["add", "--", *paths],
                action=f"Stage {len(paths)} path(s)",
                risk=RiskLevel.NORMAL,
                policy_action="git:add",
            )
        elif all_changes:
            self.repo.write(
                ["add", "--all"],
                action="Stage all changes (including untracked files)",
                risk=RiskLevel.NORMAL,
                policy_action="git:add",
            )
        if not self.engine.dry_run:
            staged = self.repo.status().staged
            if not staged and not allow_empty:
                raise ValidationError(
                    "Nothing is staged to commit.", hint="Use --all, pass paths, or stage files with git add."
                )
            forbidden = self.policy.forbidden_matches([f.path for f in staged])
            if forbidden:
                raise ValidationError(
                    "Refusing to commit files forbidden by policy",
                    details=forbidden,
                    hint="Unstage them (git restore --staged <file>).",
                )
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self.repo.write(args, action="Create commit", risk=RiskLevel.NORMAL, policy_action="git:commit")
        return self.repo.head(short=True)

    # ---------------------------------------------------------------- sync
    def sync(self, *, remote: str = "origin", push: bool = False, force_push: bool = False) -> SyncOutcome:
        """Fetch, fast-forward if behind, optionally push. Never merges or rebases silently."""
        self.repo.require()
        if remote not in self.repo.remotes():
            raise ValidationError(f"Remote '{remote}' is not configured.", hint="git remote add origin <url>")
        branch = self.repo.current_branch()
        if branch is None:
            raise ValidationError("HEAD is detached; switch to a branch before syncing.")
        self.repo.write(
            ["fetch", "--prune", remote], action=f"Fetch from {remote}", risk=RiskLevel.SAFE, policy_action="git:fetch"
        )
        status = self.repo.status()
        pulled = pushed = False
        if status.upstream is None:
            message = f"branch '{branch}' has no upstream"
        elif status.ahead and status.behind and not force_push:
            raise ValidationError(
                f"'{branch}' has diverged from {status.upstream} ({status.ahead} ahead, {status.behind} behind).",
                hint="Merge or rebase manually; HighhX never rewrites history silently.",
            )
        else:
            message = "up to date"
            if status.behind and not force_push:
                if not status.clean:
                    raise ValidationError("You have local changes; commit or stash them before pulling.")
                self.repo.write(
                    ["pull", "--ff-only", remote, branch],
                    action=f"Fast-forward {branch} from {remote}",
                    risk=RiskLevel.NORMAL,
                    policy_action="git:pull",
                )
                pulled = True
                message = f"fast-forwarded {status.behind} commit(s)"
        if push or force_push:
            protected = self.policy.is_protected_branch(branch)
            args = ["push", remote, branch]
            if status.upstream is None:
                args.insert(1, "--set-upstream")
            if force_push:
                args.insert(1, "--force-with-lease")
                risk = RiskLevel.CRITICAL
                action = f"FORCE-push '{branch}' to {remote} (rewrites remote history)"
            else:
                risk = RiskLevel.DANGEROUS
                action = f"Push '{branch}' to {remote}" + (" (protected branch)" if protected else "")
            self.repo.write(
                args,
                action=action,
                risk=risk,
                policy_action="git:force-push" if force_push else "git:push",
                bypassable=not (force_push and protected),
            )
            pushed = True
            message += "; pushed"
        after = self.repo.status() if not self.engine.dry_run else status
        return SyncOutcome(True, pulled, pushed, after.ahead, after.behind, message)

    # ---------------------------------------------------------------- tags
    def tag(self, name: str, *, message: str | None = None, push: bool = False, remote: str = "origin") -> None:
        self.repo.require()
        if self.repo.tag_exists(name):
            raise ValidationError(f"Tag '{name}' already exists.")
        self.repo.write(
            ["tag", "-a", name, "-m", message or name],
            action=f"Create tag '{name}'",
            risk=RiskLevel.NORMAL,
            policy_action="git:tag",
        )
        if push:
            if remote not in self.repo.remotes():
                raise IntegrationError(f"Remote '{remote}' is not configured; tag created locally only.")
            self.repo.write(
                ["push", remote, name],
                action=f"Push tag '{name}' to {remote}",
                risk=RiskLevel.DANGEROUS,
                policy_action="git:push-tag",
            )
