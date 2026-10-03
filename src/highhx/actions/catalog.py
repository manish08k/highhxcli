"""The action catalog: every capability HighhX can execute, Free and Pro alike."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from functools import cache
from typing import TYPE_CHECKING, Any

from highhx.actions.handlers import computer, desktop, files, native
from highhx.actions.handlers.delegate import delegate, flag, listed, opt
from highhx.actions.policy import Risk
from highhx.actions.spec import (
    BROWSER,
    CONTAINERS,
    DATABASE,
    DEPLOY,
    DESKTOP,
    GIT_LOCAL,
    GIT_REMOTE,
    NETWORK,
    READ_PROJECT,
    RUN_PROCESSES,
    WRITE_PROJECT,
    ActionSpec,
    Inputs,
)
from highhx.cloud.plans import AGENT_CODE_CHANGES, AGENT_COMMANDS, AGENT_COMPUTER_USE, AGENT_DEPLOY, AGENT_GIT
from highhx.computer.verify import MAX_TIMEOUT_MS, PREDICATES
from highhx.core.errors import HighhXError
from highhx.execution.retry import RetryPolicy
from highhx.project.index import KINDS as FILE_KINDS
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Bool, Int, List, Map, Num, Obj, Prop, Str

if TYPE_CHECKING:
    from highhx.commands import App

LONG = 24 * 3600.0
"""Timeout for foreground processes that run until stopped (dev servers)."""
READ_RETRY = RetryPolicy(attempts=3, delay=0.5, backoff=2.0, max_delay=5.0)
"""Read-only, idempotent actions may be retried with exponential backoff."""

EXIT = {"command": "the HighhX command that ran", "exit_code": "its exit code"}
NO_INPUT = Obj({})
COORD = Int(minimum=-100_000, maximum=100_000)
"""A desktop coordinate in points (negative on displays left of / above the main one)."""
BACKGROUND_APP = Prop(Str(min_length=1), description="Deliver to this application in the background (best effort).")
SERVICES_INPUT = Obj({"services": Prop(List(Str(min_length=1)), description="Service names (default: all).")})


class Catalog:
    def __init__(self, specs: list[ActionSpec]) -> None:
        self._specs: dict[str, ActionSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"duplicate action {spec.name!r}")
            self._specs[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        spec = self._specs.get(name)
        if spec is not None:
            return spec
        return next((s for s in self._specs.values() if name in s.aliases), None)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> list[str]:
        return list(self._specs)

    def categories(self) -> dict[str, list[ActionSpec]]:
        grouped: dict[str, list[ActionSpec]] = {}
        for spec in self._specs.values():
            grouped.setdefault(spec.category, []).append(spec)
        return grouped

    def for_agent(self, features: frozenset[str]) -> list[ActionSpec]:
        """Actions the AI agent may propose: offered to the agent and within the account's features."""
        return [s for s in self._specs.values() if s.agent and (s.feature is None or s.feature in features)]


def _cmd(prefix: str) -> Any:
    return lambda inputs: prefix


def _args(inputs: Inputs) -> list[str]:
    return [str(a) for a in inputs.get("args") or []]


def _specs() -> list[ActionSpec]:
    D = delegate
    specs = [
        # ---------------------------------------------------------------- project
        ActionSpec(
            "project.detect",
            "Detect the project: languages, frameworks, package managers, commands, services, workflows.",
            native.project_detect,
            outputs={
                "name": "project name",
                "type": "primary stack",
                "commands": "known commands",
                "...": "see /context",
            },
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=120,
        ),
        ActionSpec(
            "project.init",
            "Create the .highhx/ configuration for this project (never overwrites without --force).",
            D(lambda i: ["init", *flag(i, "force", "--force")]),
            Obj({"force": Prop(Bool())}),
            EXIT,
            Risk.MEDIUM,
            ActionKind.WRITE_FILE,
            (WRITE_PROJECT,),
            target=lambda i: ".highhx/",
            feature=AGENT_CODE_CHANGES,
        ),
        ActionSpec(
            "project.status",
            "Project dashboard: git, environment, services and recent runs.",
            D(lambda i: ["status"]),
            NO_INPUT,
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "project.check",
            "Run lint, type-check and tests in parallel.",
            D(lambda i: ["check"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx check"),
        ),
        ActionSpec(
            "project.test",
            "Run the project's tests with its detected test runner.",
            D(lambda i: ["test", *_args(i)]),
            Obj({"args": Prop(List(Str()), description="Extra arguments for the test runner.")}),
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=lambda i: " ".join(["highhx test", *map(shlex.quote, _args(i))]),
        ),
        ActionSpec(
            "project.fix",
            "Apply the project's formatters and linter auto-fixes (changes files).",
            D(lambda i: ["fix"]),
            NO_INPUT,
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (RUN_PROCESSES, WRITE_PROJECT),
            timeout=1800,
            feature=AGENT_CODE_CHANGES,
            command=_cmd("highhx fix"),
        ),
        ActionSpec(
            "project.build",
            "Build the project with its detected build tool.",
            D(lambda i: ["build"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES, WRITE_PROJECT),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx build"),
        ),
        ActionSpec(
            "project.run",
            "Start the development environment in the foreground (until stopped).",
            D(lambda i: ["dev"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=LONG,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx dev"),
        ),
        ActionSpec(
            "project.start",
            "Start the project's configured background services.",
            D(lambda i: ["start"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            feature=AGENT_COMMANDS,
            command=_cmd("highhx start"),
        ),
        ActionSpec(
            "project.stop",
            "Stop the project's background services.",
            D(lambda i: ["stop"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            feature=AGENT_COMMANDS,
            command=_cmd("highhx stop"),
        ),
        # ------------------------------------------------------------ filesystem
        ActionSpec(
            "filesystem.read",
            "Read a project file (or list a directory). Secret files are never read.",
            files.read,
            Obj(
                {
                    "path": Prop(Str(min_length=1), required=True),
                    "offset": Prop(Int(minimum=1)),
                    "limit": Prop(Int(minimum=1, maximum=5000)),
                }
            ),
            {"path": "relative path", "lines": "line count", "text": "content", "entries": "directory entries"},
            permissions=(READ_PROJECT,),
            target=lambda i: str(i.get("path", "")),
            idempotent=True,
            retry=READ_RETRY,
            timeout=60,
        ),
        ActionSpec(
            "filesystem.write",
            "Create or replace a project file (journaled: /undo restores it).",
            files.write,
            Obj({"path": Prop(Str(min_length=1), required=True), "content": Prop(Str(), required=True)}),
            {"path": "relative path", "created": "true for a new file", "sha256": "digest of the new content"},
            Risk.MEDIUM,
            ActionKind.WRITE_FILE,
            (WRITE_PROJECT,),
            timeout=60,
            verify=files.verify_write,
            compensate=files.undo_write,
            feature=AGENT_CODE_CHANGES,
            target=lambda i: str(i.get("path", "")),
            preview=files.preview_write,
        ),
        ActionSpec(
            "filesystem.copy",
            "Copy a file or directory within the project.",
            files.copy,
            Obj(
                {
                    "source": Prop(Str(min_length=1), required=True),
                    "destination": Prop(Str(min_length=1), required=True),
                    "overwrite": Prop(Bool()),
                }
            ),
            {"source": "copied from", "destination": "copied to", "replaced": "an existing file was replaced"},
            Risk.MEDIUM,
            ActionKind.WRITE_FILE,
            (WRITE_PROJECT,),
            timeout=300,
            compensate=files.undo_copy,
            feature=AGENT_CODE_CHANGES,
            target=lambda i: str(i.get("destination", "")),
        ),
        ActionSpec(
            "filesystem.move",
            "Move or rename a file or directory within the project.",
            files.move,
            Obj(
                {
                    "source": Prop(Str(min_length=1), required=True),
                    "destination": Prop(Str(min_length=1), required=True),
                }
            ),
            {"source": "moved from", "destination": "moved to"},
            Risk.MEDIUM,
            ActionKind.WRITE_FILE,
            (WRITE_PROJECT,),
            timeout=300,
            compensate=files.undo_move,
            feature=AGENT_CODE_CHANGES,
            target=lambda i: f"{i.get('source', '')} → {i.get('destination', '')}",
            preview=files.preview_move,
        ),
        ActionSpec(
            "filesystem.delete",
            "Delete a project file (journaled: /undo restores it) or, with recursive, a directory.",
            files.delete,
            Obj({"path": Prop(Str(min_length=1), required=True), "recursive": Prop(Bool())}),
            {"path": "deleted path", "directory": "true for a directory"},
            Risk.HIGH,
            ActionKind.DELETE_FILE,
            (WRITE_PROJECT,),
            timeout=300,
            verify=files.verify_delete,
            compensate=files.undo_delete,
            feature=AGENT_CODE_CHANGES,
            target=lambda i: str(i.get("path", "")),
            command=lambda i: f"rm -rf {shlex.quote(str(i.get('path', '')))}" if i.get("recursive") else None,
            preview=files.preview_delete,
        ),
        ActionSpec(
            "filesystem.list",
            "List a project folder (default: the project root).",
            computer.filesystem_list,
            Obj({"path": Prop(Str(min_length=1))}),
            {"path": "folder", "entries": "[{name, size}]"},
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=60,
        ),
        ActionSpec(
            "filesystem.open",
            "Open a project file or folder in an application (code in a text editor; executables are never opened).",
            computer.filesystem_open,
            Obj({"path": Prop(Str(min_length=1), required=True), "app": Prop(Str(min_length=1))}),
            {"path": "what was opened", "with": "which application"},
            Risk.LOW,
            ActionKind.APP_LAUNCH,
            (READ_PROJECT, DESKTOP),
            timeout=30,
            agent=False,
            target=lambda i: str(i.get("path", "")),
        ),
        ActionSpec(
            "filesystem.create",
            "Create a new, empty file or a folder in the project (never overwrites; /undo removes it).",
            computer.filesystem_create,
            Obj(
                {
                    "path": Prop(Str(min_length=1), required=True),
                    "kind": Prop(Str(choices=("file", "folder"))),
                    "content": Prop(Str()),
                }
            ),
            {"path": "created path", "kind": "file or folder"},
            Risk.LOW,
            ActionKind.WRITE_FILE,
            (WRITE_PROJECT,),
            timeout=30,
            verify=computer.verify_exists,
            compensate=computer.undo_create,
            feature=AGENT_CODE_CHANGES,
            target=lambda i: str(i.get("path", "")),
        ),
        ActionSpec(
            "filesystem.find",
            "Find project files by kind (pdf, image, document …), name keywords and modification time "
            "(names and dates only — contents are never read; secret files are skipped).",
            files.find,
            Obj(
                {
                    "kinds": Prop(List(Str(choices=tuple(FILE_KINDS)))),
                    "keywords": Prop(List(Str(min_length=1))),
                    "modified_after": Prop(Str(min_length=1), description="ISO date/time (inclusive)."),
                    "modified_before": Prop(Str(min_length=1), description="ISO date/time (exclusive)."),
                    "sort": Prop(Str(choices=("newest", "oldest", "name"))),
                    "limit": Prop(Int(minimum=1, maximum=200)),
                    "path": Prop(Str(min_length=1), description="Only inside this project folder."),
                }
            ),
            {"files": "[{path, name, kind, size, modified}]", "partial": "the project was too large to index fully"},
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=60,
        ),
        ActionSpec(
            "filesystem.search",
            "Search project files with a regular expression (secret files are skipped).",
            files.search,
            Obj(
                {
                    "pattern": Prop(Str(min_length=1), required=True),
                    "path": Prop(Str()),
                    "glob": Prop(Str()),
                    "ignore_case": Prop(Bool()),
                    "max_results": Prop(Int(minimum=1, maximum=500)),
                }
            ),
            {"matches": "list of {path, line, text}"},
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=120,
        ),
        # ------------------------------------------------------------------- git
        ActionSpec(
            "git.status",
            "Branch, upstream and changed files.",
            D(lambda i: ["git", "status"]),
            NO_INPUT,
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "git.diff",
            "Show uncommitted changes (full diff, or per-file counts with stat=true).",
            D(lambda i: ["git", "diff", *flag(i, "stat", "--stat"), *flag(i, "staged", "--staged")]),
            Obj({"stat": Prop(Bool()), "staged": Prop(Bool())}),
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "git.log",
            "Recent commits.",
            D(lambda i: ["git", "history", *opt(i, "limit", "-n")]),
            Obj({"limit": Prop(Int(minimum=1, maximum=500))}),
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "git.branch",
            "List branches, or create one (and switch to it).",
            D(lambda i: ["git", "branch", *([str(i["name"]), "--create"] if i.get("name") else [])]),
            Obj({"name": Prop(Str(min_length=1), description="Create this branch (omit to list).")}),
            EXIT,
            Risk.SAFE,
            ActionKind.GIT,
            (GIT_LOCAL,),
            feature=AGENT_GIT,
            command=lambda i: f"git switch -c {shlex.quote(str(i['name']))}" if i.get("name") else "git branch",
        ),
        ActionSpec(
            "git.checkout",
            "Switch to a branch or ref (create=true creates it).",
            native.git_checkout,
            Obj({"ref": Prop(Str(min_length=1), required=True), "create": Prop(Bool())}),
            {"ref": "now checked out", "previous": "branch before"},
            Risk.MEDIUM,
            ActionKind.GIT,
            (GIT_LOCAL,),
            compensate=native.undo_checkout,
            feature=AGENT_GIT,
            command=lambda i: f"git switch {'-c ' if i.get('create') else ''}{shlex.quote(str(i.get('ref', '')))}",
            policy_action=lambda i: "git:checkout",
        ),
        ActionSpec(
            "git.commit",
            "Commit changes (validated message; protected-branch rules apply).",
            native.git_commit,
            Obj(
                {
                    "message": Prop(Str(min_length=1), required=True),
                    "all": Prop(Bool(), description="Stage all changes, including untracked files."),
                    "paths": Prop(List(Str(min_length=1))),
                }
            ),
            {"commit": "new commit id", "parent": "previous HEAD"},
            Risk.MEDIUM,
            ActionKind.GIT,
            (GIT_LOCAL,),
            compensate=native.undo_commit,
            feature=AGENT_GIT,
            command=lambda i: f"git commit -m {shlex.quote(str(i.get('message', '')))}",
            policy_action=lambda i: "git:commit",
        ),
        ActionSpec(
            "git.tag",
            "Create an annotated tag.",
            D(lambda i: ["git", "tag", str(i["name"]), *opt(i, "message", "-m")]),
            Obj({"name": Prop(Str(min_length=1), required=True), "message": Prop(Str())}),
            EXIT,
            Risk.MEDIUM,
            ActionKind.GIT,
            (GIT_LOCAL,),
            compensate=native.undo_tag,
            feature=AGENT_GIT,
            command=lambda i: f"git tag -a {shlex.quote(str(i.get('name', '')))}",
            policy_action=lambda i: "git:tag",
        ),
        ActionSpec(
            "git.push",
            "Push commits (and optionally tags) to a remote.",
            native.git_push,
            Obj({"remote": Prop(Str(min_length=1)), "branch": Prop(Str(min_length=1)), "tags": Prop(Bool())}),
            {"remote": "remote pushed to"},
            Risk.HIGH,
            ActionKind.GIT,
            (GIT_REMOTE, NETWORK),
            timeout=600,
            feature=AGENT_GIT,
            command=lambda i: " ".join(
                ["git push", str(i.get("remote") or "origin"), *([str(i["branch"])] if i.get("branch") else [])]
            ),
            policy_action=lambda i: "git:push",
        ),
        ActionSpec(
            "git.pull",
            "Fast-forward from a remote.",
            native.git_pull,
            Obj({"remote": Prop(Str(min_length=1))}),
            {"before": "HEAD before", "after": "HEAD after"},
            Risk.MEDIUM,
            ActionKind.GIT,
            (GIT_REMOTE, NETWORK),
            timeout=600,
            compensate=native.undo_pull,
            feature=AGENT_GIT,
            command=lambda i: f"git pull --ff-only {i.get('remote') or 'origin'}",
            policy_action=lambda i: "git:pull",
        ),
        ActionSpec(
            "git.revert",
            "Create a commit that reverts a commit (default HEAD).",
            native.git_revert,
            Obj({"ref": Prop(Str(min_length=1))}),
            {"head": "HEAD after"},
            Risk.MEDIUM,
            ActionKind.GIT,
            (GIT_LOCAL,),
            feature=AGENT_GIT,
            command=lambda i: f"git revert --no-edit {i.get('ref') or 'HEAD'}",
            policy_action=lambda i: "git:revert",
        ),
        # --------------------------------------------------------------- package
        ActionSpec(
            "package.install",
            "Install the project's dependencies with its package manager(s).",
            D(lambda i: ["deps", "install", *opt(i, "manager", "--manager")]),
            Obj({"manager": Prop(Str(min_length=1))}),
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (RUN_PROCESSES, NETWORK, WRITE_PROJECT),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx deps install"),
        ),
        ActionSpec(
            "package.update",
            "Update dependencies (all, or the named packages).",
            D(lambda i: ["deps", "update", *listed(i, "packages"), *opt(i, "manager", "--manager")]),
            Obj({"packages": Prop(List(Str(min_length=1))), "manager": Prop(Str(min_length=1))}),
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (RUN_PROCESSES, NETWORK, WRITE_PROJECT),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=lambda i: "highhx deps update " + " ".join(listed(i, "packages")),
        ),
        ActionSpec(
            "package.audit",
            "Audit dependencies for known vulnerabilities.",
            D(lambda i: ["deps", "audit"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES, NETWORK),
            timeout=900,
            idempotent=True,
            retry=READ_RETRY,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx deps audit"),
        ),
        ActionSpec(
            "package.outdated",
            "List outdated dependencies.",
            D(lambda i: ["deps", "outdated"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES, NETWORK),
            timeout=900,
            idempotent=True,
            retry=READ_RETRY,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx deps outdated"),
        ),
        # ---------------------------------------------------------------- docker
        ActionSpec(
            "docker.build",
            "Build the compose services' images.",
            D(lambda i: ["docker", "up", "--build", *listed(i, "services")]),
            SERVICES_INPUT,
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (CONTAINERS,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("docker compose up --build"),
        ),
        ActionSpec(
            "docker.run",
            "Start the compose services (detached).",
            D(lambda i: ["docker", "up", *listed(i, "services")]),
            SERVICES_INPUT,
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (CONTAINERS,),
            timeout=1800,
            feature=AGENT_COMMANDS,
            command=_cmd("docker compose up -d"),
        ),
        ActionSpec(
            "docker.stop",
            "Stop the compose services (volumes are kept).",
            D(lambda i: ["docker", "down"]),
            NO_INPUT,
            EXIT,
            Risk.MEDIUM,
            ActionKind.EXEC,
            (CONTAINERS,),
            timeout=900,
            feature=AGENT_COMMANDS,
            command=_cmd("docker compose down"),
        ),
        ActionSpec(
            "docker.logs",
            "Show recent container logs.",
            D(lambda i: ["docker", "logs", *listed(i, "services"), *opt(i, "tail", "--tail")]),
            Obj({"services": Prop(List(Str(min_length=1))), "tail": Prop(Int(minimum=1, maximum=100000))}),
            EXIT,
            permissions=(CONTAINERS,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        # -------------------------------------------------------------- database
        ActionSpec(
            "database.connect",
            "Check the database connection and migration status.",
            D(lambda i: ["db", "status"]),
            NO_INPUT,
            EXIT,
            permissions=(DATABASE,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=120,
        ),
        ActionSpec(
            "database.migrate",
            "Apply pending database migrations.",
            D(lambda i: ["db", "migrate"]),
            NO_INPUT,
            EXIT,
            Risk.HIGH,
            ActionKind.EXEC,
            (DATABASE,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx db migrate"),
            policy_action=lambda i: "db:migrate",
        ),
        ActionSpec(
            "database.backup",
            "Back up the database.",
            D(lambda i: ["db", "backup"]),
            NO_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (DATABASE,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx db backup"),
            policy_action=lambda i: "db:backup",
        ),
        ActionSpec(
            "database.restore",
            "Restore the database from a backup (the current data is backed up first).",
            D(lambda i: ["db", "restore", *([str(i["backup"])] if i.get("backup") else [])]),
            Obj({"backup": Prop(Str(min_length=1))}),
            EXIT,
            Risk.CRITICAL,
            ActionKind.EXEC,
            (DATABASE,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=_cmd("highhx db restore"),
            policy_action=lambda i: "db:restore",
        ),
        # --------------------------------------------------------------- service
        ActionSpec(
            "service.start",
            "Start background services (all, or the named ones).",
            D(lambda i: ["start", *listed(i, "services")]),
            SERVICES_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            feature=AGENT_COMMANDS,
            command=lambda i: "highhx start " + " ".join(listed(i, "services")),
        ),
        ActionSpec(
            "service.stop",
            "Stop background services (all, or the named ones).",
            D(lambda i: ["stop", *listed(i, "services")]),
            SERVICES_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            feature=AGENT_COMMANDS,
            command=lambda i: "highhx stop " + " ".join(listed(i, "services")),
        ),
        ActionSpec(
            "service.restart",
            "Restart background services (all, or the named ones).",
            D(lambda i: ["restart", *listed(i, "services")]),
            SERVICES_INPUT,
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            feature=AGENT_COMMANDS,
            command=lambda i: "highhx restart " + " ".join(listed(i, "services")),
        ),
        ActionSpec(
            "service.logs",
            "Show a service's log, or the latest execution's log.",
            D(lambda i: ["logs", *opt(i, "service", "--service"), *opt(i, "tail", "-n")]),
            Obj({"service": Prop(Str(min_length=1)), "tail": Prop(Int(minimum=1, maximum=100000))}),
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        # --------------------------------------------------------------- browser
        ActionSpec(
            "browser.open",
            "Open a URL in the HighhX browser (its own profile) — or, with app, in that browser.",
            computer.browser_open,
            Obj(
                {
                    "url": Prop(Str(min_length=1), required=True),
                    "app": Prop(Str(min_length=1), description="A browser, e.g. Safari (opens there; not automated)."),
                }
            ),
            {"url": "page URL", "title": "page title", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER, NETWORK),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("url", "")),
            aliases=("browser.navigate",),
        ),
        ActionSpec(
            "browser.search",
            "Search a site (YouTube, GitHub, Wikipedia …) or the web for a query.",
            computer.browser_search,
            Obj(
                {
                    "query": Prop(Str(min_length=1), required=True),
                    "site": Prop(Str(min_length=1), description="A known site (default: the web)."),
                    "app": Prop(Str(min_length=1), description="Open the results in this browser."),
                }
            ),
            {"url": "results page", "query": "what was searched"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER, NETWORK),
            timeout=120,
            agent=False,
            target=lambda i: f"{i.get('site') or 'web'}: {i.get('query', '')}",
        ),
        ActionSpec(
            "browser.play",
            "Play the first result for a query on a media site (YouTube): results → first result → start it.",
            computer.browser_play,
            Obj(
                {
                    "query": Prop(Str(min_length=1), required=True),
                    "site": Prop(Str(min_length=1), description="A site that can play media (default: YouTube)."),
                }
            ),
            {"url": "what is playing", "title": "its title", "playing": "the media started"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER, NETWORK),
            timeout=180,
            agent=False,
            target=lambda i: f"{i.get('site') or 'YouTube'}: {i.get('query', '')}",
        ),
        ActionSpec(
            "browser.find",
            "Find controls on the current page whose name contains a text (targets for browser.click).",
            computer.browser_find,
            Obj({"text": Prop(Str(min_length=1), required=True)}),
            {"matches": "[{role, name, target}]"},
            permissions=(BROWSER,),
            timeout=60,
            agent=False,
        ),
        ActionSpec(
            "browser.click",
            'Click a control by role and name, e.g. "button:Save" (sensitive controls ask first).',
            native.browser_click,
            Obj({"target": Prop(Str(min_length=1), required=True), "timeout": Prop(Num(minimum=0))}),
            {"url": "page URL after", "step": "outcome"},
            Risk.LOW,
            ActionKind.UI_CLICK,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "browser.fill",
            'Type into a field by role and name, e.g. "textbox:Email" (text_from_env for secrets).',
            native.browser_fill,
            Obj(
                {
                    "target": Prop(Str(min_length=1), required=True),
                    "text": Prop(Str()),
                    "text_from_env": Prop(Str(min_length=1)),
                    "timeout": Prop(Num(minimum=0)),
                }
            ),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_TYPE,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
            aliases=("browser.type",),
        ),
        ActionSpec(
            "browser.press",
            "Press a key in the browser (enter, tab, escape …).",
            native.browser_press,
            Obj({"key": Prop(Str(min_length=1), required=True)}),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_KEY,
            (BROWSER,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,
        ),
        ActionSpec(
            "browser.wait",
            "Wait for seconds, or until text / a URL / a control appears.",
            native.browser_wait,
            Obj(
                {
                    "seconds": Prop(Num(minimum=0), description="Up to an hour."),
                    "text": Prop(Str()),
                    "url_contains": Prop(Str()),
                    "title_contains": Prop(Str()),
                    "element": Prop(Str()),
                    "timeout": Prop(Num(minimum=0)),
                }
            ),
            {"waited": "seconds", "step": "outcome"},
            permissions=(BROWSER,),
            timeout=3700,
            agent=False,
        ),
        ActionSpec(
            "browser.extract",
            "Read the current page (or open url first): title, text and controls as structured data.",
            native.browser_extract,
            Obj(
                {
                    "url": Prop(Str(min_length=1)),
                    "roles": Prop(List(Str(min_length=1))),
                    "limit": Prop(Int(minimum=1, maximum=2000)),
                    "max_text": Prop(Int(minimum=0, maximum=200000)),
                }
            ),
            {"url": "page URL", "title": "title", "text": "visible text", "elements": "[{role, name, value}]"},
            permissions=(BROWSER,),
            timeout=120,
            agent=False,
            aliases=("browser.read",),
        ),
        ActionSpec(
            "browser.screenshot",
            "Save a screenshot of the current page (default .highhx/screenshots/).",
            native.browser_screenshot,
            Obj({"path": Prop(Str(min_length=1))}),
            {"path": "saved PNG", "bytes": "size"},
            permissions=(BROWSER, WRITE_PROJECT),
            timeout=60,
            agent=False,
        ),
        ActionSpec(
            "browser.back",
            "Go back one page in the HighhX browser (to that exact history entry).",
            native.browser_back,
            NO_INPUT,
            {"url": "page URL after", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
        ),
        ActionSpec(
            "browser.forward",
            "Go forward one page in the HighhX browser.",
            native.browser_forward,
            NO_INPUT,
            {"url": "page URL after", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
        ),
        ActionSpec(
            "browser.refresh",
            "Reload the current page (never repeated automatically: a form result would be resubmitted).",
            native.browser_refresh,
            NO_INPUT,
            {"url": "page URL after", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            aliases=("browser.reload",),
        ),
        ActionSpec(
            "browser.new_tab",
            "Open a new tab (optionally at a URL), verify it exists and continue in it.",
            native.browser_new_tab,
            Obj({"url": Prop(Str(min_length=1))}),
            {"url": "page URL", "tab": "the new tab", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER, NETWORK),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("url") or "new tab"),
        ),
        ActionSpec(
            "browser.close_tab",
            "Close the current tab and continue in the most recently used remaining one.",
            native.browser_close_tab,
            NO_INPUT,
            {"url": "page URL after", "tab": "the tab continued in", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,
        ),
        ActionSpec(
            "browser.switch_tab",
            "Continue in another open tab (a tab id, or text in its URL or title).",
            native.browser_switch_tab,
            Obj({"tab": Prop(Str(min_length=1), required=True)}),
            {"url": "page URL", "tab": "the tab", "step": "outcome"},
            Risk.LOW,
            ActionKind.NAVIGATE,
            (BROWSER,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("tab", "")),
        ),
        ActionSpec(
            "browser.tabs",
            "List the HighhX browser's open tabs (id, URL, title, which one is current).",
            native.browser_tabs,
            NO_INPUT,
            {"tabs": "[{id, url, title, opener, active}]"},
            permissions=(BROWSER,),
            timeout=60,
            agent=False,
        ),
        ActionSpec(
            "browser.hover",
            'Move the pointer over a control by role and name, e.g. "link:Menu".',
            native.browser_hover,
            Obj({"target": Prop(Str(min_length=1), required=True), "timeout": Prop(Num(minimum=0))}),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_SCROLL,
            (BROWSER,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "browser.double_click",
            "Double-click a control by role and name (sensitive controls ask first).",
            native.browser_double_click,
            Obj({"target": Prop(Str(min_length=1), required=True), "timeout": Prop(Num(minimum=0))}),
            {"url": "page URL after", "step": "outcome"},
            Risk.LOW,
            ActionKind.UI_CLICK,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "browser.drag",
            "Drag one control onto another (drag and drop), both by role and name.",
            native.browser_drag,
            Obj(
                {
                    "source": Prop(Str(min_length=1), required=True),
                    "target": Prop(Str(min_length=1), required=True),
                    "timeout": Prop(Num(minimum=0)),
                }
            ),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_CLICK,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: f"{i.get('source', '')} → {i.get('target', '')}",
        ),
        ActionSpec(
            "browser.upload",
            "Choose project files in a page's file field (asks first: the files can leave the machine).",
            native.browser_upload,
            Obj(
                {
                    "target": Prop(Str(min_length=1), required=True),
                    "paths": Prop(List(Str(min_length=1), min_items=1), required=True),
                }
            ),
            {"step": "outcome"},
            Risk.MEDIUM,
            ActionKind.UI_UPLOAD,
            (BROWSER, READ_PROJECT),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: ", ".join(str(p) for p in i.get("paths") or []),
        ),
        ActionSpec(
            "browser.download",
            "Click a link or button that downloads a file, and follow it to completion (HighhX downloads folder).",
            native.browser_download,
            Obj({"target": Prop(Str(min_length=1), required=True), "timeout": Prop(Num(minimum=0))}),
            {"download": "{url, filename, path, state, bytes}", "step": "outcome"},
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            (BROWSER, NETWORK),
            timeout=900,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "computer.focus",
            "Bring an application to the front — or one exact window by id (from computer.windows).",
            desktop.focus,
            Obj(
                {"app": Prop(Str(min_length=1)), "window": Prop(Int(minimum=0))},
                check=lambda v: [] if (v.get("app") is None) != (v.get("window") is None) else ["give app or window"],
            ),
            {"app": "focused application", "window": "focused window", "frontmost": "verified"},
            Risk.LOW,
            ActionKind.APP_LAUNCH,
            (DESKTOP,),
            timeout=30,
            agent=False,
            target=lambda i: str(i.get("app") or f"window {i.get('window')}"),
        ),
        ActionSpec(
            "computer.type",
            "Type text into the frontmost application — or into `app` in the background (never into a terminal — use !command).",
            desktop.type_text,
            Obj({"text": Prop(Str(min_length=1), required=True), "app": BACKGROUND_APP}),
            {"app": "where it was typed", "characters": "how many"},
            Risk.MEDIUM,
            ActionKind.UI_TYPE,
            (DESKTOP,),
            timeout=60,
            agent=False,
            target=lambda i: f"{len(str(i.get('text', '')))} character(s)",
        ),
        ActionSpec(
            "computer.press",
            "Press a key in the frontmost application (Enter and Delete ask first; never in a terminal).",
            desktop.press,
            Obj({"key": Prop(Str(min_length=1), required=True), "app": BACKGROUND_APP}),
            {"app": "where", "key": "which key"},
            Risk.LOW,
            ActionKind.UI_KEY,
            (DESKTOP,),
            timeout=30,
            agent=False,
            target=lambda i: str(i.get("key", "")),
            risk_for=lambda i: (
                Risk.MEDIUM if str(i.get("key", "")).lower() in ("enter", "return", "delete", "backspace") else Risk.LOW
            ),
        ),
        ActionSpec(
            "computer.hotkey",
            "Press a key combination in the frontmost application, e.g. cmd+t (never in a terminal).",
            desktop.hotkey,
            Obj({"keys": Prop(Str(min_length=1), required=True), "app": BACKGROUND_APP}),
            {"app": "where", "keys": "the combination"},
            Risk.MEDIUM,
            ActionKind.UI_KEY,
            (DESKTOP,),
            timeout=30,
            agent=False,
            target=lambda i: str(i.get("keys", "")),
        ),
        ActionSpec(
            "computer.click",
            'Click a control in the frontmost application by role and name, e.g. "button:Save".',
            desktop.click,
            Obj({"target": Prop(Str(min_length=1), required=True), "timeout": Prop(Num(minimum=0))}),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=120,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "computer.scroll",
            "Scroll the HighhX browser page (default) or the frontmost application.",
            desktop.scroll,
            Obj(
                {
                    "capture": Prop(
                        Str(min_length=1), description="Coordinates are in this screenshot (from computer.screenshot)."
                    ),
                    "space": Prop(Str(choices=("pixels", "relative1000")), description="With capture: pixels or 0-1000."),
                    "direction": Prop(Str(choices=("up", "down", "left", "right"))),
                    "source": Prop(Str(choices=("browser", "desktop"))),
                    "amount": Prop(Int(minimum=1, maximum=20)),
                    "x": Prop(COORD, description="Desktop: scroll at this point (with y)."),
                    "y": Prop(COORD),
                }
            ),
            {"step": "outcome"},
            Risk.SAFE,
            ActionKind.UI_SCROLL,
            (BROWSER, DESKTOP),
            timeout=30,
            agent=False,
        ),
        ActionSpec(
            "computer.launch",
            "Launch a desktop application by name.",
            native.app_launch,
            Obj({"name": Prop(Str(min_length=1), required=True)}),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.APP_LAUNCH,
            (DESKTOP,),
            timeout=120,
            agent=False,
            target=lambda i: str(i.get("name", "")),
        ),
        # ------------------------------------------- the HighhX Computer Runtime (desktop)
        ActionSpec(
            "computer.observe",
            "Observe the desktop: the frontmost application and window, all windows, the accessibility "
            "tree (roles, names, states, bounds; secret fields are never read) and, when asked, a screenshot.",
            desktop.observe,
            Obj({"app": Prop(Str(min_length=1)), "tree": Prop(Bool()), "screenshot": Prop(Bool())}),
            {"app": "frontmost application", "windows": "[…]", "elements": "[…]", "screenshot": "{path …}"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.verify",
            "Verify bounded predicates about one exact window — it exists, its frame, an element's "
            "existence, value, enabled or selected state — sampled from fresh state until they hold "
            "stably. Each is satisfied, unsatisfied or unknown; unknown is never success. Reads only.",
            desktop.verify,
            Obj(
                {
                    "window": Prop(Int(minimum=0), description="The exact window id (from computer.windows)."),
                    "app": Prop(
                        Str(min_length=1),
                        description="Or: this application's front window (neither: the frontmost window).",
                    ),
                    "expect": Prop(PREDICATES, required=True),
                    "timeout_ms": Prop(Int(minimum=0, maximum=MAX_TIMEOUT_MS)),
                    "stable_samples": Prop(Int(minimum=1, maximum=5)),
                }
            ),
            {"status": "satisfied | unsatisfied | unknown", "predicates": "[{index, status, detail}]"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: str(i.get("window") or i.get("app") or ""),
        ),
        ActionSpec(
            "computer.screenshot",
            "Capture the screen — or one window (by id, or an application's front window), or a region "
            "[x, y, width, height] in desktop points — to a PNG in HighhX's screenshots folder.",
            desktop.screenshot,
            Obj(
                {
                    "window": Prop(Int(minimum=0)),
                    "app": Prop(Str(min_length=1)),
                    "max_size": Prop(Int(minimum=200, maximum=8000), description="Longest side in pixels (for a model)."),
                    "region": Prop(List(Int(minimum=-100_000, maximum=100_000), min_items=4)),
                },
                check=lambda v: (
                    ["region must be [x, y, width, height]"]
                    if v.get("region") is not None and len(v["region"]) != 4
                    else []
                ),
            ),
            {"path": "the PNG", "width": "pixels", "height": "pixels", "scale": "pixels per point"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.windows",
            "List on-screen windows front to back (id, application, title, bounds) and the frontmost one.",
            desktop.windows,
            Obj({"app": Prop(Str(min_length=1))}),
            {"frontmost": "application", "windows": "[{id, pid, app, title, x, y, width, height}]"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.apps",
            "List running applications with a user interface.",
            desktop.apps,
            NO_INPUT,
            {"apps": "[{name, pid, bundle_id, frontmost}]"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.element_at",
            "The accessibility element at a desktop point (application, role, name, bounds).",
            desktop.element_at,
            Obj({"x": Prop(COORD, required=True), "y": Prop(COORD, required=True)}),
            {"role": "role", "name": "accessible name", "bounds": "[x, y, width, height]"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.click_at",
            "Click at a desktop point — or on the text named by `text`, found by perception (accessibility "
            "first, OCR as the fallback). Right/middle button, double or triple click; with `background`, "
            "deliver it to `app` without bringing it to the front (best effort).",
            desktop.click_at,
            Obj(
                {
                    "capture": Prop(
                        Str(min_length=1), description="Coordinates are in this screenshot (from computer.screenshot)."
                    ),
                    "space": Prop(Str(choices=("pixels", "relative1000")), description="With capture: pixels or 0-1000."),
                    "x": Prop(COORD),
                    "y": Prop(COORD),
                    "text": Prop(Str(min_length=1, check=lambda v: "at most 200 characters" if len(v) > 200 else None)),
                    "button": Prop(Str(choices=("left", "right", "middle"))),
                    "count": Prop(Int(minimum=1, maximum=3)),
                    "app": Prop(Str(min_length=1)),
                    "background": Prop(Bool()),
                }
            ),
            {"element": "what was under the pointer", "grounded": "how `text` was found"},
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: str(i.get("text") or f"({i.get('x')}, {i.get('y')})"),
        ),
        ActionSpec(
            "computer.move",
            "Move the pointer to a desktop point (hover), and verify it is there.",
            desktop.move,
            Obj(
                {
                    "capture": Prop(
                        Str(min_length=1), description="Coordinates are in this screenshot (from computer.screenshot)."
                    ),
                    "space": Prop(Str(choices=("pixels", "relative1000")), description="With capture: pixels or 0-1000."),
                    "x": Prop(COORD, required=True),
                    "y": Prop(COORD, required=True),
                }
            ),
            {"cursor": "[x, y] after the move"},
            Risk.LOW,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: f"({i.get('x')}, {i.get('y')})",
        ),
        ActionSpec(
            "computer.mouse_button",
            "Only press (down) or only release (up) a mouse button at a desktop point — for drags and "
            "long presses computer.drag cannot express. Release what you press.",
            desktop.mouse_button,
            Obj(
                {
                    "capture": Prop(
                        Str(min_length=1), description="Coordinates are in this screenshot (from computer.screenshot)."
                    ),
                    "space": Prop(Str(choices=("pixels", "relative1000")), description="With capture: pixels or 0-1000."),
                    "action": Prop(Str(choices=("down", "up")), required=True),
                    "x": Prop(COORD, required=True),
                    "y": Prop(COORD, required=True),
                    "button": Prop(Str(choices=("left", "right", "middle"))),
                }
            ),
            {"cursor": "[x, y] after the move"},
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: f"{i.get('action')} at ({i.get('x')}, {i.get('y')})",
        ),
        ActionSpec(
            "computer.drag",
            "Press at one desktop point, move to another and release.",
            desktop.drag,
            Obj(
                {
                    "capture": Prop(
                        Str(min_length=1), description="Coordinates are in this screenshot (from computer.screenshot)."
                    ),
                    "space": Prop(Str(choices=("pixels", "relative1000")), description="With capture: pixels or 0-1000."),
                    "from_x": Prop(COORD, required=True),
                    "from_y": Prop(COORD, required=True),
                    "to_x": Prop(COORD, required=True),
                    "to_y": Prop(COORD, required=True),
                    "button": Prop(Str(choices=("left", "right", "middle"))),
                    "duration_ms": Prop(Int(minimum=0, maximum=5000)),
                }
            ),
            {"from": "[x, y]", "to": "[x, y]"},
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: f"({i.get('from_x')}, {i.get('from_y')}) → ({i.get('to_x')}, {i.get('to_y')})",
        ),
        ActionSpec(
            "computer.menu",
            'Choose an application menu item by its path, e.g. "File > Save" (never in a terminal).',
            desktop.menu,
            Obj(
                {
                    "app": Prop(Str(min_length=1), required=True),
                    "path": Prop(Str(min_length=1), required=True, description='"File > Save"'),
                }
            ),
            {"app": "application", "path": "the items chosen"},
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            (DESKTOP,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: f"{i.get('app')}: {i.get('path')}",
        ),
        ActionSpec(
            "computer.window",
            "Move and resize a window (by id, or an application's frontmost window), and verify its new frame.",
            desktop.window_frame,
            Obj(
                {
                    "window": Prop(Int(minimum=0)),
                    "app": Prop(Str(min_length=1)),
                    "x": Prop(COORD, required=True),
                    "y": Prop(COORD, required=True),
                    "width": Prop(Int(minimum=1, maximum=100_000), required=True),
                    "height": Prop(Int(minimum=1, maximum=100_000), required=True),
                }
            ),
            {"frame": "[x, y, width, height] after the change"},
            Risk.LOW,
            ActionKind.UI_SELECT,
            (DESKTOP,),
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: str(i.get("app") or i.get("window") or ""),
        ),
        ActionSpec(
            "computer.quit",
            "Ask an application to quit (it may ask to save first), and verify it did.",
            desktop.quit_app,
            Obj({"app": Prop(Str(min_length=1), required=True)}),
            {"app": "application", "quit": "whether it quit"},
            Risk.MEDIUM,
            ActionKind.APP_LAUNCH,
            (DESKTOP,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: str(i.get("app", "")),
        ),
        ActionSpec(
            "computer.clipboard_read",
            "Read the clipboard's plain text (it may hold private data: always asked for the agent).",
            desktop.clipboard_read,
            NO_INPUT,
            {"text": "the clipboard text", "characters": "its length"},
            Risk.MEDIUM,
            ActionKind.READ,
            (DESKTOP,),
            idempotent=True,
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
        ),
        ActionSpec(
            "computer.clipboard_write",
            "Replace the clipboard with plain text, and verify it by reading it back.",
            desktop.clipboard_write,
            Obj({"text": Prop(Str(min_length=1), required=True)}),
            {"characters": "how many"},
            Risk.MEDIUM,
            ActionKind.UI_TYPE,
            (DESKTOP,),
            timeout=30,
            feature=AGENT_COMPUTER_USE,
            agent=False,  # the agent reaches these through computer_act (entitled by tool name)
            target=lambda i: f"{len(str(i.get('text', '')))} character(s)",
        ),
        # ------------------------------------------------------------ deployment
        ActionSpec(
            "deployment.deploy",
            "Deploy to a configured target (preflight checks run first; production asks for typed approval).",
            D(lambda i: ["deploy", "to", *([str(i["environment"])] if i.get("environment") else [])]),
            Obj({"environment": Prop(Str(min_length=1), description="Deploy target / environment name.")}),
            EXIT,
            Risk.HIGH,
            ActionKind.DEPLOY,
            (DEPLOY, NETWORK),
            timeout=7200,
            feature=AGENT_DEPLOY,
            target=lambda i: str(i.get("environment") or "default target"),
            environment=lambda i: str(i["environment"]) if i.get("environment") else None,
            policy_action=lambda i: f"deploy:{i.get('environment') or 'default'}",
        ),
        ActionSpec(
            "deployment.rollback",
            "Roll a target back to its previous deployment.",
            D(lambda i: ["rollback", *([str(i["environment"])] if i.get("environment") else [])]),
            Obj({"environment": Prop(Str(min_length=1))}),
            EXIT,
            Risk.HIGH,
            ActionKind.ROLLBACK,
            (DEPLOY, NETWORK),
            timeout=3600,
            feature=AGENT_DEPLOY,
            target=lambda i: str(i.get("environment") or "default target"),
            environment=lambda i: str(i["environment"]) if i.get("environment") else None,
            policy_action=lambda i: f"rollback:{i.get('environment') or 'default'}",
        ),
        ActionSpec(
            "deployment.status",
            "Deployment status of a target (or all).",
            D(lambda i: ["deploy", "status", *([str(i["environment"])] if i.get("environment") else [])]),
            Obj({"environment": Prop(Str(min_length=1))}),
            EXIT,
            permissions=(DEPLOY,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "deployment.logs",
            "Logs of a target's deployment.",
            D(lambda i: ["deploy", "logs", *([str(i["environment"])] if i.get("environment") else [])]),
            Obj({"environment": Prop(Str(min_length=1))}),
            EXIT,
            permissions=(DEPLOY,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        # -------------------------------------------------------------- security
        ActionSpec(
            "security.scan",
            "Scan for secrets, risky permissions, config and dependency issues.",
            D(lambda i: ["security", "scan"], ok_codes=(0, 9)),
            NO_INPUT,
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
            timeout=1800,
        ),
        ActionSpec(
            "security.doctor",
            "Check tools, configuration, environment variables and ports.",
            D(lambda i: ["doctor"]),
            NO_INPUT,
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        ActionSpec(
            "security.diagnose",
            "Diagnose why the project is failing (recent errors, services, environment).",
            D(lambda i: ["diagnose"]),
            NO_INPUT,
            EXIT,
            permissions=(READ_PROJECT,),
            idempotent=True,
            retry=READ_RETRY,
        ),
        # -------------------------------------------------------------- workflow
        ActionSpec(
            "workflow.run",
            "Run a workflow (each step is classified and approved as it runs).",
            D(
                lambda i: [
                    "run",
                    str(i["name"]),
                    *[x for k, v in (i.get("inputs") or {}).items() for x in ("-i", f"{k}={v}")],
                ]
            ),
            Obj({"name": Prop(Str(min_length=1), required=True), "inputs": Prop(Map(Str()))}),
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=LONG,
            feature=AGENT_COMMANDS,
            target=lambda i: str(i.get("name", "")),
            command=lambda i: f"highhx run {i.get('name', '')}",
        ),
        ActionSpec(
            "workflow.resume",
            "Resume a failed or cancelled workflow run from its first unfinished step.",
            D(lambda i: ["workflow", "resume", str(i["execution_id"])]),
            Obj({"execution_id": Prop(Str(min_length=1), required=True)}),
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=LONG,
            feature=AGENT_COMMANDS,
            command=lambda i: f"highhx workflow resume {i.get('execution_id', '')}",
        ),
        ActionSpec(
            "workflow.cancel",
            "Cancel a running workflow (in any HighhX process).",
            D(lambda i: ["workflow", "cancel", str(i["execution_id"])]),
            Obj({"execution_id": Prop(Str(min_length=1), required=True)}),
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=60,
            feature=AGENT_COMMANDS,
            command=lambda i: f"highhx workflow cancel {i.get('execution_id', '')}",
        ),
        # ----------------------------------------------------------------- shell
        ActionSpec(
            "shell.run",
            "Run a shell command in the project (classified by what it does; dangerous ones always ask).",
            D(lambda i: ["exec", "--shell", str(i["command"])]),
            Obj({"command": Prop(Str(min_length=1), required=True)}),
            EXIT,
            Risk.LOW,
            ActionKind.EXEC,
            (RUN_PROCESSES,),
            timeout=3600,
            feature=AGENT_COMMANDS,
            command=lambda i: str(i.get("command", "")),
            policy_action=lambda i: f"exec:{(str(i.get('command', '')).split() or ['shell'])[0]}",
        ),
    ]
    from highhx.actions.catalog_computer import computer_use_specs

    return [*specs, *computer_use_specs()]


@cache
def default_catalog() -> Catalog:
    return Catalog(_specs())


def plugin_actions(app: App) -> list[ActionSpec]:
    """Actions for the enabled plugins' declared commands: ``plugin.<plugin>.<command>``.

    Boundaries: a plugin's declared risk can only raise the floor — never below LOW, and the
    classifier still rates the concrete command; plugin actions are never offered to the AI
    agent; they run through the plugin command itself (isolated environment, plugin policy name).
    """
    try:
        registry = app.plugins
        commands = dict(registry.declarative_commands)
    except HighhXError:
        return []
    specs: list[ActionSpec] = []
    for name, (manifest, command) in sorted(commands.items()):
        floor = max(Risk.parse(command.risk or "normal"), Risk.LOW)
        specs.append(
            ActionSpec(
                f"plugin.{manifest.name}.{name}",
                f"{command.description or command.run} (plugin {manifest.name} {manifest.version})",
                delegate(_plugin_argv(name)),
                Obj({"args": Prop(List(Str()), description="Arguments appended to the plugin command.")}),
                EXIT,
                floor,
                ActionKind.EXEC,
                (RUN_PROCESSES,),
                timeout=3600,
                agent=False,
                command=_plugin_command(command.run),
                policy_action=_fixed_policy(f"plugin:{manifest.name}:{name}"),
            )
        )
    return specs


def _plugin_argv(name: str) -> Callable[[Inputs], list[str]]:
    return lambda inputs: [name, *_args(inputs)]


def _plugin_command(run: str) -> Callable[[Inputs], str]:
    return lambda inputs: " ".join([run, *map(shlex.quote, _args(inputs))])


def _fixed_policy(name: str) -> Callable[[Inputs], str]:
    return lambda inputs: name


def catalog_for(app: App) -> Catalog:
    """The built-in catalog plus this project's plugin actions (for the user's own actions)."""
    extra = plugin_actions(app)
    return Catalog([*default_catalog(), *extra]) if extra else default_catalog()
