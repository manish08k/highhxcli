"""File permission checks (POSIX only; Windows ACLs are not inspected)."""

from __future__ import annotations

import stat
from pathlib import Path

from highhx.security.report import Finding
from highhx.utils.platform import supports_posix_permissions

PRIVATE_KEY_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
PRIVATE_KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _is_env_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample", ".env.template"))


def check_permissions(root: Path, files: list[Path]) -> list[Finding]:
    if not supports_posix_permissions():
        return []
    findings: list[Finding] = []
    candidates = set(files)
    candidates.update(p for p in root.glob(".env*") if p.is_file())
    for path in sorted(candidates):
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        sensitive = path.name in PRIVATE_KEY_NAMES or path.suffix in PRIVATE_KEY_SUFFIXES or _is_env_file(path.name)
        if mode & stat.S_IWOTH:
            findings.append(
                Finding(
                    "world-writable",
                    "high" if sensitive else "medium",
                    "File is writable by any user",
                    "permissions",
                    rel,
                    detail=f"mode {oct(mode)}",
                    remediation=f"chmod o-w {rel}",
                )
            )
        if sensitive and mode & (stat.S_IRGRP | stat.S_IROTH):
            findings.append(
                Finding(
                    "sensitive-file-readable",
                    "medium",
                    "Sensitive file is readable by other users",
                    "permissions",
                    rel,
                    detail=f"mode {oct(mode)}",
                    remediation=f"chmod 600 {rel} (or run `highhx repair`)",
                )
            )
    hooks = root / ".highhx" / "hooks"
    if hooks.is_dir():
        for hook in hooks.iterdir():
            try:
                mode = stat.S_IMODE(hook.stat().st_mode)
            except OSError:
                continue
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                findings.append(
                    Finding(
                        "hook-writable",
                        "high",
                        "Hook script is writable by other users",
                        "permissions",
                        hook.relative_to(root).as_posix(),
                        remediation="chmod go-w on the hook",
                    )
                )
    return findings
