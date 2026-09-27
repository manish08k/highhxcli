"""Read-only tools for understanding a project."""

from __future__ import annotations

import re
from typing import Any

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate
from highhx.project.status import collect_status
from highhx.utils.filesystem import DEFAULT_IGNORE_DIRS, is_binary_file, iter_files, matches_any
from highhx.utils.validation import Bool, Int, Obj, Prop, Str

MAX_READ_BYTES = 2_000_000
IGNORE_DIRS = DEFAULT_IGNORE_DIRS - {".highhx"} | {".git"}


class ProjectOverviewTool(Tool):
    name = "project_overview"
    label = "Inspecting project"
    description = """
Detected facts about the project: stacks, frameworks, package managers, the effective
lint/test/build/typecheck/format commands, git state, environment, services, recent
HighhX runs and configured deploy targets. Start here when you need the big picture.
"""

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        app = ctx.app
        status = collect_status(app)
        profile = app.profile.to_dict()
        data = {
            **status,
            "detection": {k: profile.get(k) for k in ("languages", "frameworks", "package_managers", "databases")},
            "commands": app.commands(),
            "workflows": app.workflow_loader.keys() if app.initialized else [],
            "deploy_targets": sorted(app.config.deploy_targets),
            "highhx_initialized": app.initialized,
        }
        stacks = ", ".join(status["project"].get("stacks") or []) or "unknown stack"
        return ToolResult.json(data, summary=f"{status['project'].get('name')} · {stacks}")


class ListFilesTool(Tool):
    name = "list_files"
    label = "Listing files"
    description = """
List files and directories under a project path (dependency, build and VCS directories
are skipped). Use `glob` to filter by pattern, e.g. '**/*.py' or 'src/**/test_*.ts'.
"""
    schema = Obj(
        {
            "path": Prop(Str(), description="Directory relative to the project root (default '.')."),
            "glob": Prop(Str(), description="fnmatch-style pattern on the relative path."),
            "max_depth": Prop(Int(minimum=1, maximum=20), description="Default 4."),
            "limit": Prop(Int(minimum=1, maximum=2000), description="Maximum entries (default 400)."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"List {args.get('glob') or args.get('path') or 'files'}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        base = ctx.permissions.resolve(str(args.get("path") or "."), must_exist=True)
        if not base.is_dir():
            raise ToolError(f"{ctx.permissions.relative(base)} is not a directory")
        depth = int(args.get("max_depth") or 4)
        limit = int(args.get("limit") or 400)
        pattern = args.get("glob")
        root = ctx.permissions.root.resolve()
        entries: list[str] = []
        truncated = False
        for path in iter_files(base, ignore_dirs=IGNORE_DIRS):
            rel = path.relative_to(root).as_posix()
            if len(path.relative_to(base).parts) > depth or ctx.permissions.is_secret(rel):
                continue
            if pattern and not (matches_any(rel, [pattern]) or matches_any(path.name, [pattern])):
                continue
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(rel)
        text = "\n".join(entries) or "(no files)"
        if truncated:
            text += f"\n… more files not shown (limit {limit}); narrow with path or glob"
        return ToolResult(text, summary=f"{len(entries)} file(s)")


class ReadFileTool(Tool):
    name = "read_file"
    label = "Reading"
    description = """
Read a text file from the project with line numbers. For large files read a window with
`offset` (1-based first line) and `limit` (number of lines). Secret files (.env, keys)
are never readable.
"""
    schema = Obj(
        {
            "path": Prop(Str(min_length=1), required=True),
            "offset": Prop(Int(minimum=1), description="First line to read (default 1)."),
            "limit": Prop(Int(minimum=1, maximum=5000), description="Number of lines (default 800)."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Read {args.get('path')}"

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = ctx.permissions.resolve(str(args["path"]), must_exist=True)
        rel = ctx.permissions.relative(path)
        if path.is_dir():
            raise ToolError(f"{rel} is a directory; use list_files")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"{rel} is larger than {MAX_READ_BYTES // 1_000_000} MB; search it with search_code")
        if is_binary_file(path):
            raise ToolError(f"{rel} is a binary file")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = int(args.get("offset") or 1)
        limit = int(args.get("limit") or 800)
        window = lines[offset - 1 : offset - 1 + limit]
        width = len(str(offset + len(window)))
        body = "\n".join(f"{n:>{width}}  {line}" for n, line in enumerate(window, start=offset))
        body = ctx.app.redactor.redact(body)
        more = offset - 1 + len(window) < len(lines)
        footer = f"\n… ({len(lines)} lines total; continue with offset={offset + len(window)})" if more else ""
        return ToolResult(
            truncate(f"{rel} ({len(lines)} lines)\n{body}{footer}"), summary=f"{rel} · {len(window)} lines"
        )


class SearchCodeTool(Tool):
    name = "search_code"
    label = "Searching code"
    description = """
Search project files with a regular expression (Python syntax). Returns matching lines as
path:line: text. Use it to find definitions, usages, error messages and configuration.
"""
    schema = Obj(
        {
            "pattern": Prop(Str(min_length=1), required=True, description="Regular expression."),
            "path": Prop(Str(), description="Directory or file to search (default '.')."),
            "glob": Prop(Str(), description="Only files matching this pattern, e.g. '*.py'."),
            "ignore_case": Prop(Bool()),
            "max_results": Prop(Int(minimum=1, maximum=1000), description="Default 200."),
        }
    )

    def describe(self, args: dict[str, Any]) -> str:
        return f"Search {args.get('pattern')!r}" + (f" in {args['glob']}" if args.get("glob") else "")

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        try:
            regex = re.compile(str(args["pattern"]), re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from None
        base = ctx.permissions.resolve(str(args.get("path") or "."), must_exist=True)
        root = ctx.permissions.root.resolve()
        pattern = args.get("glob")
        limit = int(args.get("max_results") or 200)
        files = [base] if base.is_file() else iter_files(base, ignore_dirs=IGNORE_DIRS, max_size=MAX_READ_BYTES)
        hits: list[str] = []
        scanned = 0
        for path in files:
            if ctx.cancel.cancelled:
                break
            rel = path.relative_to(root).as_posix()
            if ctx.permissions.is_secret(rel):
                continue
            if pattern and not (matches_any(rel, [pattern]) or matches_any(path.name, [pattern])):
                continue
            if is_binary_file(path):
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{number}: {line.strip()[:300]}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        body = ctx.app.redactor.redact("\n".join(hits)) if hits else f"No matches in {scanned} file(s)."
        if len(hits) >= limit:
            body += f"\n… stopped at {limit} matches; narrow the search"
        return ToolResult(truncate(body), summary=f"{len(hits)} match(es)")
