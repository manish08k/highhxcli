"""Git hook installation (hooks call back into HighhX)."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.core.errors import NotFoundError, ValidationError
from highhx.utils.filesystem import atomic_write_text

GIT_HOOKS = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-push",
    "post-checkout",
    "post-merge",
    "pre-rebase",
)
MARKER = "# managed by highhx"


def hook_script(name: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{MARKER}\n"
        "# Remove with: highhx hook uninstall " + name + "\n"
        "if command -v highhx >/dev/null 2>&1; then\n"
        f'  exec highhx hook run {name} -- "$@"\n'
        "fi\n"
        "for py in python3 python; do\n"
        '  if command -v "$py" >/dev/null 2>&1 && "$py" -c \'import highhx\' >/dev/null 2>&1; then\n'
        f'    exec "$py" -m highhx hook run {name} -- "$@"\n'
        "  fi\n"
        "done\n"
        'echo "highhx: not found; skipping ' + name + ' hook" >&2\n'
        "exit 0\n"
    )


@dataclass
class HookInfo:
    name: str
    configured: str | None
    installed: bool
    managed: bool
    script: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def git_hooks_dir(root: Path) -> Path:
    git = root / ".git"
    if git.is_file():  # worktree / submodule: "gitdir: <path>"
        content = git.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            git = (root / content.split(":", 1)[1].strip()).resolve()
    if not git.is_dir():
        raise NotFoundError("Not a git repository (no .git directory).", hint="Run `git init` first.")
    return git / "hooks"


def is_managed(path: Path) -> bool:
    return path.is_file() and MARKER in path.read_text(encoding="utf-8", errors="replace")


def install_hook(root: Path, name: str, *, force: bool = False) -> Path:
    if name not in GIT_HOOKS:
        raise ValidationError(f"Unknown git hook '{name}'.", hint=f"Supported: {', '.join(GIT_HOOKS)}")
    path = git_hooks_dir(root) / name
    if path.exists() and not is_managed(path):
        if not force:
            raise ValidationError(
                f"A {name} hook already exists and was not created by HighhX.",
                hint="Use --force to back it up (to .bak) and replace it.",
            )
        path.replace(path.with_suffix(".bak"))
    atomic_write_text(path, hook_script(name))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def uninstall_hook(root: Path, name: str) -> bool:
    path = git_hooks_dir(root) / name
    if not is_managed(path):
        return False
    path.unlink()
    backup = path.with_suffix(".bak")
    if backup.exists():
        backup.replace(path)
    return True


def local_hook_scripts(hooks_dir: Path, name: str) -> list[Path]:
    """Extra scripts in ``.highhx/hooks/`` named ``<hook>`` or ``<hook>.*``."""
    if not hooks_dir.is_dir():
        return []
    return sorted(p for p in hooks_dir.iterdir() if p.is_file() and (name in (p.name, p.stem)))


def hook_infos(root: Path, configured: dict[str, str], hooks_dir: Path) -> list[HookInfo]:
    try:
        git_dir: Path | None = git_hooks_dir(root)
    except NotFoundError:
        git_dir = None
    infos = []
    for name in GIT_HOOKS:
        path = git_dir / name if git_dir else None
        installed = bool(path and path.exists())
        scripts = local_hook_scripts(hooks_dir, name)
        if not (configured.get(name) or installed or scripts):
            continue
        infos.append(
            HookInfo(
                name,
                configured.get(name),
                installed,
                bool(path and is_managed(path)),
                ", ".join(s.name for s in scripts) or None,
            )
        )
    return infos
