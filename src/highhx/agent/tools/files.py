"""Code-change tools: write, edit and delete project files.

Every change is confined to the project, shown as a diff, approved according to
the agent's approval mode and project policy (``agent:write`` / ``agent:delete``),
recorded in the change journal (so ``/undo`` can revert a turn) and written
atomically. ``--dry-run`` shows the diff without writing.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult
from highhx.approvals.risk import RiskLevel
from highhx.cloud.plans import AGENT_CODE_CHANGES
from highhx.safety.actions import ActionDescriptor, ActionKind
from highhx.utils.filesystem import atomic_write_text, is_binary_file
from highhx.utils.validation import Bool, Obj, Prop, Str

MAX_WRITE_BYTES = 2_000_000


@dataclass
class FileChange:
    path: Path
    before: str | None
    """``None`` when the file did not exist."""
    after: str | None
    """``None`` when the file was deleted."""


@dataclass
class ChangeJournal:
    """Original contents of every file the agent changed, grouped per user turn."""

    turns: list[list[FileChange]] = field(default_factory=list)

    def begin_turn(self) -> None:
        self.turns.append([])

    def record(self, change: FileChange) -> None:
        if not self.turns:
            self.begin_turn()
        self.turns[-1].append(change)

    def last_changes(self) -> list[FileChange]:
        for turn in reversed(self.turns):
            if turn:
                return turn
        return []

    def undo_last(self) -> list[Path]:
        """Restore files changed in the most recent turn that changed files."""
        while self.turns and not self.turns[-1]:
            self.turns.pop()
        if not self.turns:
            return []
        restored: list[Path] = []
        for change in reversed(self.turns.pop()):
            if change.before is None:
                if change.path.exists():
                    change.path.unlink()
            else:
                atomic_write_text(change.path, change.before)
            invalidate_bytecode(change.path)
            restored.append(change.path)
        return list(dict.fromkeys(restored))

    def changed_paths(self) -> list[Path]:
        return list(dict.fromkeys(c.path for turn in self.turns for c in turn))


def invalidate_bytecode(path: Path) -> None:
    """Drop cached bytecode for a Python file we just changed.

    CPython validates ``.pyc`` files by source mtime (1-second resolution) and
    size, so an edit that keeps the size and lands within the same second as the
    previous compile would otherwise run stale code — exactly what happens when
    the agent fixes ``a - b`` → ``a + b`` and immediately re-runs the tests.
    """
    if path.suffix != ".py":
        return
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(f"{path.stem}.*.pyc"):
            with contextlib.suppress(OSError):
                pyc.unlink()


def unified_diff(rel: str, before: str | None, after: str | None, *, context: int = 3) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{rel}" if before is not None else "/dev/null",
        tofile=f"b/{rel}" if after is not None else "/dev/null",
        n=context,
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def diff_stats(diff: str) -> tuple[int, int]:
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _current(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_dir():
        raise ToolError("path is a directory")
    if is_binary_file(path):
        raise ToolError("refusing to modify a binary file")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("file is not UTF-8 text; refusing to rewrite it") from None


class _FileTool(Tool):
    mutating = True
    feature = AGENT_CODE_CHANGES
    risk = RiskLevel.NORMAL

    def apply(self, ctx: ToolContext, path: Path, before: str | None, after: str | None, verb: str) -> ToolResult:
        perms = ctx.permissions
        rel = perms.relative(path)
        diff = unified_diff(rel, before, after)
        added, removed = diff_stats(diff)

        def describe(current: str | None) -> ActionDescriptor:
            # The content hashes bind an approval to this exact change of this exact file version.
            return perms.action(
                ActionKind.DELETE_FILE if after is None else ActionKind.WRITE_FILE,
                f"{verb} {rel} (+{added} -{removed})",
                tool=self.name,
                target=rel,
                before_sha=_sha(current),
                after_sha=_sha(after),
            )

        authorization = perms.authorize(
            describe(before),
            policy_action="agent:delete" if after is None else "agent:write",
            grant="edit",
            details=[diff] if diff else [],
        )
        if ctx.app.options.dry_run:
            return ToolResult(f"[dry-run] would {verb.lower()} {rel}:\n{diff}", summary=f"{rel} (dry run)")
        with perms.executing(authorization, describe(_current(path))) as event:
            if after is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, after)
            invalidate_bytecode(path)
            ctx.journal.record(FileChange(path, before, after))
            verified = (not path.exists()) if after is None else _current(path) == after
            event.verified = verified
            if not verified:
                event.status = "failed"
        if not verified:
            return ToolResult.error(f"{verb} {rel} could not be verified: the file does not have the expected content.")
        return ToolResult(
            f"{verb} {rel}: +{added} -{removed} lines (verified).",
            summary=f"{rel} +{added} -{removed}",
            changed_files=[rel],
            data={"diff": diff},
            verified=True,
        )


def _sha(text: str | None) -> str:
    return "absent" if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


class WriteFileTool(_FileTool):
    name = "write_file"
    label = "Writing"
    description = """
Create a file or replace its entire contents. Prefer edit_file for changes to existing
files. Paths are relative to the project root; parent directories are created.
"""
    schema = Obj(
        {
            "path": Prop(Str(min_length=1), required=True),
            "content": Prop(Str(), required=True, description="The complete new file contents."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Write {args.get('path')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        content = str(args["content"])
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError("content is too large")
        path = ctx.permissions.resolve(str(args["path"]), write=True)
        before = _current(path)
        if before == content:
            return ToolResult(f"{ctx.permissions.relative(path)} already has this content.", summary="unchanged")
        return self.apply(ctx, path, before, content, "Create" if before is None else "Rewrite")


class EditFileTool(_FileTool):
    name = "edit_file"
    label = "Editing"
    description = """
Replace an exact snippet in a file. `old_text` must match the file exactly (including
indentation) and be unique unless replace_all is true — include enough surrounding lines
to make it unique. Read the file first. Use an empty old_text only to create a new file.
"""
    schema = Obj(
        {
            "path": Prop(Str(min_length=1), required=True),
            "old_text": Prop(Str(), required=True, description="Exact existing text to replace."),
            "new_text": Prop(Str(), required=True, description="Replacement text."),
            "replace_all": Prop(Bool(), description="Replace every occurrence (default false)."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Edit {args.get('path')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.permissions.resolve(str(args["path"]), write=True)
        old, new = str(args["old_text"]), str(args["new_text"])
        before = _current(path)
        if before is None:
            if old:
                raise ToolError(f"{ctx.permissions.relative(path)} does not exist")
            return self.apply(ctx, path, None, new, "Create")
        if not old:
            raise ToolError("old_text is empty; to rewrite the whole file use write_file")
        if old == new:
            raise ToolError("old_text and new_text are identical")
        count = before.count(old)
        if count == 0:
            hint = ""
            stripped = old.strip()
            if stripped and stripped in before:
                hint = " (it matches after trimming whitespace — check indentation and line endings)"
            raise ToolError(f"old_text was not found in {ctx.permissions.relative(path)}{hint}; read the file again")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(
                f"old_text occurs {count} times; add surrounding lines to make it unique or set replace_all"
            )
        after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
        return self.apply(ctx, path, before, after, "Edit")


class DeleteFileTool(_FileTool):
    name = "delete_file"
    label = "Deleting"
    description = "Delete a single project file (not directories). Always requires the user's approval."
    schema = Obj({"path": Prop(Str(min_length=1), required=True)})

    def describe(self, args: dict[str, Any]) -> str:
        return f"Delete {args.get('path')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.permissions.resolve(str(args["path"]), write=True, must_exist=True)
        if path.is_dir():
            raise ToolError("delete_file only deletes files")
        return self.apply(ctx, path, _current(path), None, "Delete")
