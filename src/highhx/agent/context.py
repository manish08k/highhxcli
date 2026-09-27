"""Project context gathered once per session and given to the model up front."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.errors import HighhXError
from highhx.utils.filesystem import DEFAULT_IGNORE_DIRS

if TYPE_CHECKING:
    from highhx.commands import App

INSTRUCTION_FILES = ("HIGHHX.md", "AGENTS.md")
MAX_INSTRUCTIONS = 20_000
TREE_LIMIT = 150


@dataclass
class ProjectContext:
    name: str
    root: Path
    initialized: bool
    stacks: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    branch: str | None = None
    clean: bool | None = None
    changes: int = 0
    tree: list[str] = field(default_factory=list)
    instructions: list[tuple[str, str]] = field(default_factory=list)
    deploy_targets: list[str] = field(default_factory=list)
    remote: str | None = None
    """``origin`` URL, used only to derive a non-reversible project fingerprint for sync."""

    @property
    def stack_label(self) -> str:
        parts = [s.capitalize() if s.islower() else s for s in self.stacks]
        for fw in self.frameworks:
            if fw.lower() not in {s.lower() for s in parts}:
                parts.append(fw)
        return ", ".join(parts[:4]) or "Unknown"

    @property
    def git_label(self) -> str:
        if self.branch is None:
            return "not a git repository"
        return "clean" if self.clean else f"{self.changes} uncommitted change{'s' if self.changes != 1 else ''}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": str(self.root),
            "initialized": self.initialized,
            "stacks": self.stacks,
            "frameworks": self.frameworks,
            "package_managers": self.package_managers,
            "commands": self.commands,
            "branch": self.branch,
            "clean": self.clean,
            "changes": self.changes,
            "files": len(self.tree),
            "instructions": [name for name, _ in self.instructions],
            "deploy_targets": self.deploy_targets,
        }

    def render(self) -> str:
        """The project section of the system prompt (stable for the whole session)."""
        lines = [
            f"Project: {self.name}",
            f"Root: {self.root}",
            f"HighhX initialized: {'yes' if self.initialized else 'no (suggest `highhx init` if they want workflows)'}",
            f"Stack: {self.stack_label}",
        ]
        if self.package_managers:
            lines.append(f"Package managers: {', '.join(self.package_managers)}")
        if self.commands:
            lines.append("Commands (configured or detected):")
            lines += [f"  {k}: {v}" for k, v in sorted(self.commands.items())]
        lines.append(f"Git: {self.branch or '-'} ({self.git_label})")
        if self.deploy_targets:
            lines.append(f"Deploy targets: {', '.join(self.deploy_targets)}")
        if self.tree:
            lines.append(f"Files (first {len(self.tree)}, ignoring dependencies and build output):")
            lines += [f"  {p}" for p in self.tree]
        for name, text in self.instructions:
            lines += ["", f"Project instructions from {name}:", text]
        return "\n".join(lines)


def _tree(root: Path, limit: int = TREE_LIMIT) -> list[str]:
    """Shallow-first listing so the top of the project is always visible."""
    from highhx.agent.permissions import AgentPermissions

    out: list[str] = []
    frontier = [root]
    while frontier and len(out) < limit:
        next_frontier: list[Path] = []
        for directory in frontier:
            try:
                children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                continue
            for child in children:
                if child.name in DEFAULT_IGNORE_DIRS or (
                    child.name.startswith(".")
                    and child.name
                    not in (
                        ".github",
                        ".highhx",
                        ".env.example",
                    )
                ):
                    continue
                if child.is_symlink():
                    continue
                rel = child.relative_to(root).as_posix()
                if AgentPermissions.is_secret(rel) or rel.startswith((".highhx/state", ".highhx/logs")):
                    continue
                out.append(rel + ("/" if child.is_dir() else ""))
                if child.is_dir():
                    next_frontier.append(child)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        frontier = next_frontier
    return sorted(out)


def gather(app: App) -> ProjectContext:
    profile = app.profile
    ctx = ProjectContext(
        name=app.config.project_name or profile.name or app.root.name,
        root=app.root,
        initialized=app.initialized,
        stacks=list(profile.stacks),
        frameworks=[d.name for d in profile.frameworks],
        package_managers=[d.name for d in profile.package_managers],
        commands=app.commands(),
        deploy_targets=sorted(app.config.deploy_targets),
    )
    try:
        repo = app.git_repo
        if repo.is_repo():
            status = repo.status()
            ctx.branch, ctx.clean, ctx.changes = status.branch, status.clean, status.change_count
            ctx.remote = repo.read("config", "--get", "remote.origin.url", allow_fail=True).strip() or None
    except HighhXError:
        pass
    ctx.tree = _tree(app.root)
    for name in INSTRUCTION_FILES:
        path = app.root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:MAX_INSTRUCTIONS]
            except OSError:
                continue
            ctx.instructions.append((name, app.redactor.redact(text)))
    return ctx
