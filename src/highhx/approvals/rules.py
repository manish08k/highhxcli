"""Built-in command risk rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from highhx.approvals.risk import RiskLevel


@dataclass
class RiskRule:
    """A regex that assigns a risk level to matching commands."""

    id: str
    pattern: str
    risk: RiskLevel
    reason: str
    bypassable: bool = True
    _regex: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._regex = re.compile(self.pattern, re.IGNORECASE)

    def matches(self, command: str) -> bool:
        return bool(self._regex.search(command))


_R = RiskLevel

# Start of a command word: line start, a separator, or an opening quote/paren/backtick
# (so `bash -c "rm -rf /"` and `$(rm …)` are recognised too).
_B = r"(?:^|[\s;&|(\"'`])"
# A path argument that is the filesystem root or the home directory.
_ROOT_TARGET = r"\s(?:/\*?|~/?|\$HOME/?|\$\{HOME\}/?|/\*)(?=$|[\s;&|\"'`)])"

BUILTIN_RULES: tuple[RiskRule, ...] = (
    # Read-only / safe commands.
    RiskRule(
        "git-read",
        # Only a single, plain git read command is SAFE — anything chained stays NORMAL or higher.
        r"^\s*git\s+(status|diff|log|show|branch\s*$|remote\s+-v|rev-parse|describe|ls-files|fetch)\b[^;&|`$()<>]*$",
        _R.SAFE,
        "read-only git command",
    ),
    # Filesystem destruction.
    RiskRule(
        "rm-recursive",
        _B + r"rm\s+(?:-[a-zA-Z]+\s+)*(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b",
        _R.DANGEROUS,
        "recursive delete",
    ),
    RiskRule(
        "rm-root",
        _B + r"rm(?=[^;&|]*\s(?:-[a-zA-Z]*[rRf]|--recursive|--force))\s[^;&|]*?" + _ROOT_TARGET,
        _R.CRITICAL,
        "deletes the root or home directory",
        bypassable=False,
    ),
    RiskRule(
        "find-delete",
        _B + r"find\s[^;&|]*(-delete\b|-exec\s+rm\b)",
        _R.DANGEROUS,
        "deletes files found by find",
    ),
    RiskRule(
        "fork-bomb",
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        _R.CRITICAL,
        "fork bomb",
        bypassable=False,
    ),
    RiskRule(
        "block-device-write",
        r">\s*/dev/(sd[a-z]|hd[a-z]|nvme\d|disk\d|mmcblk\d|xvd[a-z]|vd[a-z])",
        _R.CRITICAL,
        "overwrites a disk device",
        bypassable=False,
    ),
    RiskRule(
        "destroy-file-content",
        _B + r"(shred\s|truncate\s+(-s|--size))",
        _R.DANGEROUS,
        "destroys file contents",
    ),
    RiskRule(
        "win-delete",
        r"(^|[\s;&|])(rmdir|rd)\s+/s\b|(^|[\s;&|])del\s+/[fsq]",
        _R.DANGEROUS,
        "recursive delete (Windows)",
    ),
    RiskRule(
        "format-disk",
        r"(^|[\s;&|])(mkfs(\.\w+)?|format\s+[a-z]:|diskpart)\b",
        _R.CRITICAL,
        "formats a disk",
        bypassable=False,
    ),
    RiskRule("dd-device", r"(^|[\s;&|])dd\s+.*of=/dev/", _R.CRITICAL, "writes directly to a device", bypassable=False),
    RiskRule("chmod-777", r"chmod\s+(-R\s+)?0?777\b", _R.DANGEROUS, "makes files world-writable"),
    RiskRule("sudo", r"(^|[\s;&|])sudo\s", _R.DANGEROUS, "runs with elevated privileges"),
    RiskRule(
        "pipe-to-shell",
        r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",
        _R.DANGEROUS,
        "pipes a download into a shell",
    ),
    # Git.
    RiskRule("git-push", r"\bgit\s+push\b", _R.DANGEROUS, "pushes to a remote repository"),
    RiskRule(
        "git-force-push",
        r"\bgit\s+push\b.*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)",
        _R.CRITICAL,
        "force-pushes (rewrites remote history)",
    ),
    RiskRule(
        "git-reset-hard", r"\bgit\s+reset\s+.*--hard\b|\bgit\s+reset\s+--hard\b", _R.DANGEROUS, "discards local changes"
    ),
    RiskRule("git-clean", r"\bgit\s+clean\s+-[a-z]*[fdx]", _R.DANGEROUS, "deletes untracked files"),
    RiskRule("git-branch-delete", r"\bgit\s+branch\s+-D\b", _R.DANGEROUS, "force-deletes a branch"),
    RiskRule("git-history-rewrite", r"\bgit\s+(filter-branch|filter-repo)\b", _R.CRITICAL, "rewrites git history"),
    # Databases.
    RiskRule("sql-drop", r"\bdrop\s+(database|schema|table)\b", _R.CRITICAL, "drops database objects"),
    RiskRule("sql-truncate", r"\btruncate\s+(table\s+)?\w", _R.CRITICAL, "truncates tables"),
    RiskRule("sql-delete-all", r"\bdelete\s+from\s+[\w.`\"]+\s*(;|$|[\"'])", _R.CRITICAL, "deletes all rows"),
    RiskRule("db-restore", r"\b(pg_restore|mysql\s+.*<|psql\s+.*<)", _R.DANGEROUS, "overwrites database contents"),
    # Containers / infrastructure.
    RiskRule(
        "docker-prune", r"\bdocker\s+(system|volume|image|container)\s+prune\b", _R.DANGEROUS, "deletes Docker data"
    ),
    RiskRule(
        "compose-down-volumes",
        r"\bdocker(-compose|\s+compose)\s+down\b.*(\s-v\b|--volumes)",
        _R.CRITICAL,
        "deletes Docker volumes",
    ),
    RiskRule(
        "docker-remove",
        r"\bdocker\s+((container|image|volume|network)\s+)?(rm|rmi|prune)\b",
        _R.DANGEROUS,
        "removes Docker containers, images or volumes",
    ),
    RiskRule("kubectl-delete", r"\bkubectl\s+delete\b", _R.CRITICAL, "deletes Kubernetes resources"),
    RiskRule(
        "kubectl-apply",
        r"\bkubectl\s+(apply|replace|rollout\s+undo|scale)\b",
        _R.DANGEROUS,
        "changes Kubernetes resources",
    ),
    RiskRule("helm-change", r"\bhelm\s+(upgrade|install|uninstall|rollback)\b", _R.DANGEROUS, "changes a Helm release"),
    RiskRule("terraform-apply", r"\bterraform\s+apply\b", _R.CRITICAL, "changes infrastructure"),
    RiskRule("terraform-destroy", r"\bterraform\s+destroy\b", _R.CRITICAL, "destroys infrastructure", bypassable=False),
    # Publishing.
    RiskRule(
        "publish",
        r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\b(uv|poetry)\s+publish\b|\b(dart|flutter)\s+pub\s+publish\b|\bmvn\s+deploy\b|\bcargo\s+publish\b|\bgradlew?(\.bat)?\s+publish\b",
        _R.CRITICAL,
        "publishes a package publicly",
    ),
    # Package installation.
    RiskRule(
        "package-install",
        r"\b(pip|pip3|uv\s+pip|npm|pnpm|yarn|poetry|bun)\s+(install|add|i|sync)\b",
        _R.NORMAL,
        "installs packages",
    ),
    RiskRule(
        "global-install",
        r"\b(npm|pnpm|yarn)\s+(install|add|i)\s+(-g|--global)\b|\bpip3?\s+install\s+(--user|--break-system-packages)",
        _R.DANGEROUS,
        "installs packages globally",
    ),
    # System control.
    RiskRule(
        "shutdown",
        r"(^|[\s;&|])(shutdown|reboot|halt|poweroff)\b",
        _R.CRITICAL,
        "shuts down the machine",
        bypassable=False,
    ),
    RiskRule("kill-all", r"(^|[\s;&|])(killall|pkill)\s", _R.DANGEROUS, "kills processes by name"),
)
