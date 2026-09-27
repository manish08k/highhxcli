"""Git tools built on HighhX's GitRepository / GitManager (protected branches, forbidden files and
approval rules for push apply)."""

from __future__ import annotations

from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate
from highhx.approvals.risk import RiskLevel
from highhx.cloud.plans import AGENT_GIT
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Bool, Int, List, Obj, Prop, Str


def _repo(ctx: ToolContext) -> Any:
    repo = ctx.app.git_repo
    if not repo.is_repo():
        raise ToolError("This project is not a git repository.")
    return repo


class GitStatusTool(Tool):
    name = "git_status"
    label = "Checking git status"
    description = "Current branch, upstream, ahead/behind and staged / unstaged / untracked files."

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        status = _repo(ctx).status()
        data = status.to_dict()
        return ToolResult.json(
            data,
            summary=f"{status.branch or 'detached'} · {'clean' if status.clean else f'{status.change_count} changed'}",
        )


class GitDiffTool(Tool):
    name = "git_diff"
    label = "Reading diff"
    description = (
        "Show the diff of uncommitted changes (or staged changes, or against a revision), optionally for some paths."
    )
    schema = Obj(
        {
            "staged": Prop(Bool()),
            "revision": Prop(Str(), description="e.g. 'main' or 'HEAD~3'."),
            "paths": Prop(List(Str())),
        }
    )

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        revision = args.get("revision")
        if revision and (str(revision).startswith("-") or any(c in str(revision) for c in " ;|&$`")):
            raise ToolError("invalid revision")
        paths = [str(p) for p in args.get("paths") or []]
        for p in paths:
            ctx.permissions.resolve(p)
        text = _repo(ctx).diff_text(staged=bool(args.get("staged")), revision=revision, paths=paths or None)
        text = ctx.app.redactor.redact(text)
        return ToolResult(truncate(text or "(no differences)"), summary=f"{text.count(chr(10))} diff lines")


class GitLogTool(Tool):
    name = "git_log"
    label = "Reading history"
    description = "Recent commits (hash, author, date, subject)."
    schema = Obj({"limit": Prop(Int(minimum=1, maximum=200), description="Default 20.")})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        commits = _repo(ctx).log(limit=int(args.get("limit") or 20))
        lines = [f"{c.short} {c.date} {c.author}: {c.subject}" for c in commits]
        return ToolResult("\n".join(lines) or "(no commits)", summary=f"{len(lines)} commit(s)")


class GitCommitTool(Tool):
    name = "git_commit"
    label = "Committing"
    mutating = True
    feature = AGENT_GIT
    risk = RiskLevel.NORMAL
    description = """
Create a commit. Stage specific `paths`, or `all` changes (including untracked files), then
commit with `message` (use Conventional Commits, e.g. 'fix(api): handle empty payload').
Project policy applies: forbidden files are refused and protected branches may be blocked.
Only commit when the user asked for it or approved a plan that includes it.
"""
    schema = Obj(
        {
            "message": Prop(Str(min_length=1), required=True),
            "paths": Prop(List(Str(min_length=1))),
            "all": Prop(Bool()),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Commit: {str(args.get('message', '')).splitlines()[0][:60]}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        _repo(ctx)
        paths = [ctx.permissions.relative(ctx.permissions.resolve(str(p))) for p in args.get("paths") or []]
        message = str(args["message"])
        perms = ctx.permissions
        repo = _repo(ctx)
        before = repo.head()
        command = "git commit " + ("--all " if args.get("all") else "") + f"-m {message.splitlines()[0]!r}"
        action = perms.action(
            ActionKind.GIT,
            f"Commit: {message.splitlines()[0]}",
            tool=self.name,
            target=", ".join(paths) or ".",
            command=command,
        )
        authorization = perms.authorize(
            action,
            policy_action="agent:git-commit",
            grant="git-commit",
            details=[
                f"paths: {', '.join(paths)}" if paths else ("stage: all changes" if args.get("all") else "staged files")
            ],
        )
        with perms.executing(authorization) as event:
            sha = ctx.app.git.commit(message, all_changes=bool(args.get("all")), paths=paths or None)
            event.verified = repo.head() != before
        return ToolResult(f"Created commit {sha}.", summary=f"commit {sha}", verified=event.verified)


class GitBranchTool(Tool):
    name = "git_branch"
    label = "Creating branch"
    mutating = True
    feature = AGENT_GIT
    risk = RiskLevel.NORMAL
    description = "Create a new branch (from the current HEAD or `start`) and switch to it."
    schema = Obj({"name": Prop(Str(min_length=1), required=True), "start": Prop(Str())})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        _repo(ctx)
        name = str(args["name"])
        perms = ctx.permissions
        action = perms.action(
            ActionKind.GIT, f"Create branch {name}", tool=self.name, target=name, command=f"git switch -c {name}"
        )
        authorization = perms.authorize(action, policy_action="agent:git-branch", grant="git-branch")
        with perms.executing(authorization) as event:
            ctx.app.git.create_branch(name, start=args.get("start"), switch=True)
            event.verified = _repo(ctx).current_branch() == name
        return ToolResult(f"Created and switched to {name}.", summary=f"on {name}", verified=event.verified)


class GitPushTool(Tool):
    name = "git_push"
    label = "Pushing"
    mutating = True
    feature = AGENT_GIT
    risk = RiskLevel.DANGEROUS
    description = """
Fetch, fast-forward if behind, and push the current branch to the remote. Pushing shares
work with others, so HighhX always asks the user (and policy can forbid it). Never force-pushes.
"""
    schema = Obj({"remote": Prop(Str(), description="Default 'origin'.")})

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        repo = _repo(ctx)
        remote = str(args.get("remote") or "origin")
        if remote.startswith("-") or not remote.strip():
            raise ToolError("invalid remote name")
        branch = repo.current_branch() or "HEAD"
        perms = ctx.permissions
        action = perms.action(
            ActionKind.GIT,
            f"Push {branch} to {remote}",
            tool=self.name,
            target=f"{remote}/{branch}",
            command=f"git push {remote} {branch}",
        )
        authorization = perms.authorize(action, policy_action="agent:git-push")
        with perms.executing(authorization) as event:
            outcome = ctx.app.git.sync(remote=remote, push=True)
            event.verified = outcome.pushed
        return ToolResult.json(outcome.to_dict(), summary="pushed" if outcome.pushed else "nothing pushed")
