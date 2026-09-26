"""Release orchestration: version bump → changelog → commit → tag (→ push)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.schema import ReleaseConfig
from highhx.core.engine import Engine
from highhx.core.errors import NotFoundError, PolicyViolationError, ValidationError
from highhx.core.result import CommandResult
from highhx.execution.command import CommandSpec
from highhx.git.history import Commit
from highhx.git.repository import GitRepository
from highhx.git.tags import latest_version_tag
from highhx.policy.engine import PolicyEngine
from highhx.project.detector import ProjectProfile
from highhx.release.changelog import insert_section, render_section
from highhx.release.publisher import publish_command
from highhx.release.versioning import (
    SemVer,
    VersionFile,
    current_version,
    discover_version_files,
    suggest_bump,
    write_version,
)


@dataclass
class ReleasePlan:
    current: str | None
    next: str
    bump: str
    tag: str
    commits: list[Commit] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    changelog: str = ""
    changelog_path: str = ""
    problems: list[str] = field(default_factory=list)
    since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "next": self.next,
            "bump": self.bump,
            "tag": self.tag,
            "since": self.since,
            "commits": len(self.commits),
            "files": self.files,
            "changelog_path": self.changelog_path,
            "changelog": self.changelog,
            "problems": self.problems,
        }


class ReleaseManager:
    def __init__(
        self,
        engine: Engine,
        root: Path,
        repo: GitRepository,
        config: ReleaseConfig,
        profile: ProjectProfile,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.engine = engine
        self.root = root
        self.repo = repo
        self.config = config
        self.profile = profile
        self.policy = policy or PolicyEngine()

    def version_files(self) -> list[VersionFile]:
        return discover_version_files(self.root, self.config.version_files)

    def current(self) -> tuple[SemVer | None, list[str], str]:
        """Current version, problems, and where it came from."""
        files = self.version_files()
        version, problems = current_version(files)
        if version is not None:
            return version, problems, ", ".join(f.path.name for f in files)
        if self.repo.is_repo():
            tag = latest_version_tag(self.repo.tags(), self.config.tag_prefix)
            if tag is not None:
                return SemVer.parse(tag.name[len(self.config.tag_prefix) :]), problems, f"tag {tag.name}"
        return None, problems, "none"

    def commits_since_last_release(self) -> tuple[list[Commit], str | None]:
        if not self.repo.is_repo() or self.repo.head() is None:
            return [], None
        tag = latest_version_tag(self.repo.tags(), self.config.tag_prefix)
        revision = f"{tag.name}..HEAD" if tag else None
        return self.repo.log(limit=None, revision=revision), tag.name if tag else None

    def plan(self, bump: str = "auto", *, explicit: str | None = None) -> ReleasePlan:
        current, problems, _source = self.current()
        commits, since = self.commits_since_last_release()
        if explicit:
            try:
                target = SemVer.parse(explicit)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            chosen = "explicit"
        else:
            chosen = suggest_bump(commits) if bump == "auto" else bump
            base = current or SemVer(0, 0, 0)
            target = base.bump(chosen) if current else SemVer(0, 1, 0)
        if current is not None and target <= current:
            problems.append(f"new version {target} is not greater than current {current}")
        tag = f"{self.config.tag_prefix}{target}"
        if self.repo.is_repo() and self.repo.tag_exists(tag):
            problems.append(f"tag {tag} already exists")
        return ReleasePlan(
            current=str(current) if current else None,
            next=str(target),
            bump=chosen,
            tag=tag,
            commits=commits,
            files=[f.path.name for f in self.version_files()],
            changelog=render_section(str(target), commits),
            changelog_path=self.config.changelog,
            problems=problems,
            since=since,
        )

    def preflight(self) -> list[str]:
        problems: list[str] = []
        if not self.repo.is_repo():
            return ["not a git repository"]
        if self.repo.head() is None:
            problems.append("repository has no commits yet")
        status = self.repo.status()
        if not status.clean:
            problems.append(f"working tree has {status.change_count} uncommitted change(s)")
        branch = self.repo.current_branch()
        if not self.policy.release_branch_allowed(branch):
            problems.append(f"releases are not allowed from branch '{branch}' (policy allowed_release_branches)")
        if status.behind:
            problems.append(f"branch is {status.behind} commit(s) behind its upstream")
        return problems

    def bump_files(self, version: SemVer) -> list[str]:
        files = self.version_files()
        if not files:
            raise NotFoundError("No version file found.", hint="Set release.version_files in .highhx/config.yaml.")
        self.engine.approve(
            f"Set version {version} in {', '.join(f.path.name for f in files)}",
            RiskLevel.NORMAL,
            policy_action="release:version",
        )
        if self.engine.dry_run:
            return [f.path.name for f in files]
        return write_version(files, version)

    def release(
        self, bump: str = "auto", *, explicit: str | None = None, push: bool | None = None, allow_dirty: bool = False
    ) -> ReleasePlan:
        plan = self.plan(bump, explicit=explicit)
        preflight = self.preflight()
        if allow_dirty:
            preflight = [p for p in preflight if "uncommitted" not in p]
        problems = [*preflight, *plan.problems]
        if problems:
            if any("not allowed from branch" in p for p in problems):
                raise PolicyViolationError("Release blocked by policy", details=problems)
            raise ValidationError(
                "Release preflight failed",
                details=problems,
                hint="Fix the problems above (or use --allow-dirty for local changes).",
            )
        should_push = self.config.push if push is None else push
        details = [
            f"version: {plan.current or '(none)'} → {plan.next} ({plan.bump})",
            f"files: {', '.join(plan.files) or '(none)'}",
            f"changelog: {plan.changelog_path} ({len(plan.commits)} commit(s) since {plan.since or 'the beginning'})",
            f"commit + tag: {plan.tag}",
        ] + (["push: commit and tag to origin"] if should_push else [])
        self.engine.approve(f"Release {plan.tag}", RiskLevel.DANGEROUS, details=details, policy_action="release")
        if self.engine.dry_run:
            return plan
        with self.engine.operation("release", plan.tag, metadata={"version": plan.next}):
            files = self.version_files()
            changed = write_version(files, SemVer.parse(plan.next)) if files else []
            changelog = self.root / self.config.changelog
            insert_section(changelog, plan.changelog)
            to_add = [
                *[f.path.relative_to(self.root).as_posix() for f in files if f.path.name in changed],
                changelog.relative_to(self.root).as_posix(),
            ]
            self.repo.write(
                ["add", "--", *to_add], action="Stage release files", risk=RiskLevel.NORMAL, policy_action="git:add"
            )
            message = self.config.commit_message.format(tag=plan.tag, version=plan.next)
            self.repo.write(
                ["commit", "-m", message],
                action=f"Commit release {plan.tag}",
                risk=RiskLevel.NORMAL,
                policy_action="git:commit",
            )
            self.repo.write(
                ["tag", "-a", plan.tag, "-m", f"Release {plan.tag}"],
                action=f"Tag {plan.tag}",
                risk=RiskLevel.NORMAL,
                policy_action="git:tag",
            )
            if should_push:
                branch = self.repo.current_branch() or "HEAD"
                self.repo.write(
                    ["push", "--follow-tags", "origin", branch],
                    action=f"Push release {plan.tag}",
                    risk=RiskLevel.DANGEROUS,
                    policy_action="git:push",
                )
        return plan

    def publish(self) -> CommandResult:
        command, problems = publish_command(self.root, self.profile, self.config.publish_command)
        if command is None or problems:
            raise ValidationError("Cannot publish", details=problems)
        current, _, _ = self.current()
        self.engine.approve(
            f"Publish {self.profile.name} {current or ''} to a public registry".replace("  ", " "),
            RiskLevel.CRITICAL,
            details=[command, "Published versions usually cannot be deleted."],
            confirm_word=self.profile.name,
            policy_action="publish",
        )
        result = self.engine.run(
            CommandSpec(command, cwd=self.root, name="publish"), approved=True, policy_action="publish"
        )
        self.engine.raise_for(result)
        return result
