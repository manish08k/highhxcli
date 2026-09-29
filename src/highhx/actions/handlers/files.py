"""filesystem.* — confined to the project (same rules as the AI agent: no secret files,
no HighhX state, no files the project policy forbids), journaled for /undo, verified by
reading back, and compensable for workflow rollback."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.permissions import HIDDEN_DIRS, is_secret_path, relative_to_root
from highhx.agent.permissions import confine_path as _confine_path
from highhx.agent.tools.base import ToolError
from highhx.agent.tools.files import FileChange, invalidate_bytecode
from highhx.core.errors import HighhXError
from highhx.safety.actions import Actor
from highhx.utils.filesystem import atomic_write_text

MAX_READ_BYTES = 2_000_000
MAX_WRITE_BYTES = 5_000_000
MAX_SEARCH_RESULTS = 500
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".highhx", ".mypy_cache"}


def _confine(ctx: ActionContext, raw: str, *, write: bool = False, must_exist: bool = False) -> Path:
    """Project confinement (see :func:`highhx.agent.permissions.confine_path`), worded for the actor."""
    return _confine_path(ctx.app, raw, write=write, must_exist=must_exist, for_agent=ctx.actor == Actor.AGENT)


def _rel(ctx: ActionContext, path: Path) -> str:
    return relative_to_root(ctx.app.root, path)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    path = _confine(ctx, str(inputs["path"]), must_exist=True)
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith(".git"))
        return ActionResult(
            True, output={"path": _rel(ctx, path), "entries": entries}, summary=f"{len(entries)} entries"
        )
    if path.stat().st_size > MAX_READ_BYTES:
        raise ToolError(f"{_rel(ctx, path)} is larger than {MAX_READ_BYTES // 1_000_000} MB")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    first = max(1, int(inputs.get("offset") or 1))
    limit = int(inputs.get("limit") or 400)
    shown = lines[first - 1 : first - 1 + limit]
    return ActionResult(
        True,
        output={"path": _rel(ctx, path), "lines": len(lines), "offset": first, "text": "\n".join(shown)},
        summary=f"{_rel(ctx, path)} · {len(lines)} lines",
    )


def write(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    path = _confine(ctx, str(inputs["path"]), write=True)
    content = str(inputs["content"])
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError("content is too large")
    before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    if path.is_dir():
        raise ToolError(f"{_rel(ctx, path)} is a directory")
    ctx.journal.record(FileChange(path, before, content))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)
    invalidate_bytecode(path)
    rel = _rel(ctx, path)
    return ActionResult(
        True,
        output={"path": rel, "created": before is None, "sha256": _digest(content), "bytes": len(content.encode())},
        summary=("created " if before is None else "wrote ") + rel,
        changed=[rel],
    )


def preview_write(app: Any, inputs: Inputs) -> list[str]:
    """The unified diff the write would make (new files: their first lines)."""
    import difflib

    path = _confine_path(app, str(inputs["path"]), write=True)
    rel = relative_to_root(app.root, path)
    before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    after = str(inputs.get("content", ""))
    if before == after:
        return [f"{rel} is unchanged"]
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}" if path.is_file() else "/dev/null",
            tofile=f"b/{rel}",
        )
    )
    return [diff]


def preview_delete(app: Any, inputs: Inputs) -> list[str]:
    path = _confine_path(app, str(inputs["path"]), write=True, must_exist=True)
    rel = relative_to_root(app.root, path)
    if path.is_dir():
        count = sum(1 for p in path.rglob("*") if p.is_file())
        return [f"deletes {rel}/ and the {count} file(s) in it — this cannot be undone"]
    lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return [f"deletes {rel} ({lines} lines) — /undo can restore it"]


def preview_move(app: Any, inputs: Inputs) -> list[str]:
    source = relative_to_root(app.root, _confine_path(app, str(inputs["source"]), must_exist=True))
    dest = relative_to_root(app.root, _confine_path(app, str(inputs["destination"]), write=True))
    return [f"{source} → {dest}"]


def verify_write(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> tuple[bool, str]:
    path = _confine(ctx, str(inputs["path"]), write=True)
    actual = _digest(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else ""
    return actual == result.output.get("sha256"), "file contents differ from what was written"


def _restore(ctx: ActionContext, rel: str) -> str:
    """Put back the journaled original of ``rel`` (the latest record for it)."""
    path = _confine(ctx, rel, write=True)
    for turn in reversed(ctx.journal.turns):
        for change in reversed(turn):
            if change.path == path:
                if change.before is None:
                    path.unlink(missing_ok=True)
                    return f"removed {rel}"
                atomic_write_text(path, change.before)
                invalidate_bytecode(path)
                return f"restored {rel}"
    return f"no earlier version of {rel} recorded"


def undo_write(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    return _restore(ctx, str(result.output["path"]))


def copy(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    source = _confine(ctx, str(inputs["source"]), must_exist=True)
    dest = _confine(ctx, str(inputs["destination"]), write=True)
    if dest.exists() and not inputs.get("overwrite"):
        raise ToolError(f"{_rel(ctx, dest)} exists (set overwrite to replace it)")
    existed = dest.exists()
    if source.is_dir():
        if existed:
            raise ToolError("copying a directory onto an existing path is not supported")
        shutil.copytree(source, dest)
    else:
        ctx.journal.record(
            FileChange(dest, dest.read_text(encoding="utf-8", errors="replace") if existed else None, None)
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    rel = _rel(ctx, dest)
    return ActionResult(
        True,
        output={"source": _rel(ctx, source), "destination": rel, "directory": source.is_dir(), "replaced": existed},
        summary=f"copied {_rel(ctx, source)} → {rel}",
        changed=[rel],
    )


def undo_copy(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    dest = _confine(ctx, str(result.output["destination"]), write=True)
    if result.output.get("directory"):
        shutil.rmtree(dest, ignore_errors=True)
        return f"removed {result.output['destination']}"
    return _restore(ctx, str(result.output["destination"]))


def move(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    source = _confine(ctx, str(inputs["source"]), write=True, must_exist=True)
    dest = _confine(ctx, str(inputs["destination"]), write=True)
    if dest.exists():
        raise ToolError(f"{_rel(ctx, dest)} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return ActionResult(
        True,
        output={"source": _rel(ctx, source), "destination": _rel(ctx, dest)},
        summary=f"moved {_rel(ctx, source)} → {_rel(ctx, dest)}",
        changed=[_rel(ctx, source), _rel(ctx, dest)],
    )


def undo_move(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    source = _confine(ctx, str(result.output["source"]), write=True)
    dest = _confine(ctx, str(result.output["destination"]), write=True, must_exist=True)
    shutil.move(str(dest), str(source))
    return f"moved {result.output['destination']} back to {result.output['source']}"


def delete(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    path = _confine(ctx, str(inputs["path"]), write=True, must_exist=True)
    rel = _rel(ctx, path)
    if path.is_dir():
        if not inputs.get("recursive"):
            raise ToolError(f"{rel} is a directory (set recursive to delete it and everything in it)")
        shutil.rmtree(path)
        return ActionResult(True, output={"path": rel, "directory": True}, summary=f"deleted {rel}/", changed=[rel])
    ctx.journal.record(FileChange(path, path.read_text(encoding="utf-8", errors="replace"), None))
    path.unlink()
    invalidate_bytecode(path)
    return ActionResult(True, output={"path": rel, "directory": False}, summary=f"deleted {rel}", changed=[rel])


def verify_delete(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> tuple[bool, str]:
    return not (ctx.app.root / str(result.output["path"])).exists(), "the path still exists"


def undo_delete(ctx: ActionContext, inputs: Inputs, result: ActionResult) -> str:
    if result.output.get("directory"):
        return "compensation failed: deleted directories cannot be restored"
    return _restore(ctx, str(result.output["path"]))


def project_files(ctx: ActionContext, base: Path, glob: str = "*") -> list[Path]:
    """Files under ``base`` that git would consider part of the project: tracked and untracked,
    never ignored (git's own .gitignore semantics). Outside a git repository: a walk that
    skips dependency and build directories."""
    import fnmatch

    root = ctx.app.root.resolve()
    try:
        repo = ctx.app.git_repo
        if repo.is_repo():
            listed = repo.read("ls-files", "--cached", "--others", "--exclude-standard", "-z", allow_fail=True)
            paths = [root / name for name in listed.split("\0") if name]
            return sorted(
                p
                for p in paths
                if (p == base or p.is_relative_to(base)) and fnmatch.fnmatch(p.name, glob) and p.is_file()
            )
    except HighhXError:
        pass
    return sorted(p for p in base.rglob(glob) if not (set(p.relative_to(root).parts) & SKIP_DIRS))


def find(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Project files by kind, name keywords and modification time — metadata only, never contents
    (the same index the resolver uses: no secret files, no dependency folders, no HighhX state)."""
    from datetime import datetime

    from highhx.project.index import FileQuery, ProjectFileIndex

    base = _confine(ctx, str(inputs.get("path") or "."), must_exist=True)

    def epoch(key: str) -> float | None:
        raw = inputs.get(key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw)).timestamp()
        except ValueError:
            raise ToolError(f"{key} must be an ISO date/time, got {raw!r}") from None

    query = FileQuery(
        kinds=tuple(str(k) for k in inputs.get("kinds") or ()),
        keywords=tuple(str(k) for k in inputs.get("keywords") or ()),
        modified_after=epoch("modified_after"),
        modified_before=epoch("modified_before"),
        sort=str(inputs.get("sort") or "newest"),
        under=_rel(ctx, base),
        limit=int(inputs.get("limit") or 20),
    )
    index = ProjectFileIndex(ctx.app.root)
    files = [f.to_dict() for f in index.find(query)]
    shown = ", ".join(f["path"] for f in files[:5]) + (f", … ({len(files) - 5} more)" if len(files) > 5 else "")
    summary = f"{len(files)} file(s): {shown}" if files else "no matching files"
    return ActionResult(True, output={"files": files, "partial": index.partial}, summary=summary)


def search(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    base = _confine(ctx, str(inputs.get("path") or "."), must_exist=True)
    flags = re.IGNORECASE if inputs.get("ignore_case") else 0
    try:
        pattern = re.compile(str(inputs["pattern"]), flags)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from None
    glob = str(inputs.get("glob") or "*")
    limit = int(inputs.get("max_results") or 200)
    matches: list[dict[str, object]] = []
    files = [base] if base.is_file() else project_files(ctx, base, glob)
    for path in files:
        if len(matches) >= min(limit, MAX_SEARCH_RESULTS):
            break
        rel = _rel(ctx, path)
        parts = set(Path(rel).parts)
        if not path.is_file() or parts & SKIP_DIRS or any(rel.startswith(d) for d in HIDDEN_DIRS):
            continue
        if is_secret_path(rel) or path.stat().st_size > MAX_READ_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append({"path": rel, "line": number, "text": line.strip()[:240]})
                if len(matches) >= limit:
                    break
    return ActionResult(True, output={"matches": matches}, summary=f"{len(matches)} match(es)")
