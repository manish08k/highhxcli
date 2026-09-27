"""Deterministic safety classification — independent of any AI model.

An action is classified from its structure, not only its wording:

* **commands** are parsed into segments (``&&``, ``;``, ``|``, ``sh -c``, ``sudo``) and
  each program is classified by its semantics (git push --force, DROP TABLE through
  psql, terraform destroy, package installs, credential/security changes, remote
  code execution …), on top of HighhX's built-in risk rules;
* **UI actions** use the element's role and type (submit buttons, password fields,
  forms that POST, destructive styling, link targets) as well as its label in
  several languages;
* **files** consider what the path controls (policies, CI, hooks) and reversibility;
* **deployments** consider the environment.

The verdict says how risky the action is, whether it needs explicit human
confirmation, whether it is irreversible, and whether it is blocked outright.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field

from highhx.approvals.risk import RiskLevel, classify_command
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor

# Categories that always require explicit confirmation.
FINANCIAL = "financial"
DESTRUCTIVE = "destructive"
PUBLISH = "publish"
SUBMIT = "submit"
CONFIRM = "confirm"
INSTALL = "install"
ACCOUNT_SECURITY = "account_security"
CREDENTIAL = "credential"
PERMISSION = "permission"
SECURITY_CONTROL = "security_control"
PRODUCTION = "production"
DATABASE_DESTRUCTIVE = "database_destructive"
REMOTE_CODE = "remote_code"
DATA_EGRESS = "data_egress"
PRIVILEGE = "privilege"

SENSITIVE = frozenset(
    {
        FINANCIAL,
        DESTRUCTIVE,
        PUBLISH,
        SUBMIT,
        CONFIRM,
        INSTALL,
        ACCOUNT_SECURITY,
        CREDENTIAL,
        PERMISSION,
        SECURITY_CONTROL,
        PRODUCTION,
        DATABASE_DESTRUCTIVE,
        REMOTE_CODE,
        DATA_EGRESS,
        PRIVILEGE,
    }
)
IRREVERSIBLE = frozenset({FINANCIAL, DESTRUCTIVE, PUBLISH, SUBMIT, DATABASE_DESTRUCTIVE, PRODUCTION})


@dataclass
class SafetyVerdict:
    risk: RiskLevel
    categories: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False
    """Never allowed through automation (e.g. deleting the filesystem root)."""
    agent_blocked: bool = False
    """Never allowed when the AI proposes it (e.g. typing into a password field)."""
    risk_label: str | None = None
    """How the caller rates the risk on its own scale (shown to the person), if different."""

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.categories & SENSITIVE) or self.risk >= RiskLevel.DANGEROUS

    @property
    def irreversible(self) -> bool:
        return bool(self.categories & IRREVERSIBLE)

    def add(self, category: str, reason: str, risk: RiskLevel) -> None:
        self.categories.add(category)
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.risk = max(self.risk, risk)


# ------------------------------------------------------------------ UI wording
# Labels are normalised to lower case; entries match whole words or phrases.
UI_VERBS: dict[str, tuple[str, ...]] = {
    FINANCIAL: (
        "pay",
        "pay now",
        "purchase",
        "buy",
        "buy now",
        "checkout",
        "check out",
        "place order",
        "order now",
        "subscribe",
        "donate",
        "transfer",
        "withdraw",
        "send money",
        "add to cart and pay",
        "upgrade",
        "pagar",
        "comprar",
        "payer",
        "acheter",
        "bezahlen",
        "kaufen",
        "zahlen",
        "acquista",
        "paga",
        "購入",
        "支払",
        "支払う",
        "购买",
        "支付",
        "付款",
    ),
    DESTRUCTIVE: (
        "delete",
        "remove",
        "erase",
        "destroy",
        "drop",
        "wipe",
        "discard",
        "purge",
        "clear all",
        "empty trash",
        "reset",
        "factory reset",
        "uninstall",
        "deactivate",
        "terminate",
        "revoke",
        "close account",
        "eliminar",
        "borrar",
        "supprimer",
        "effacer",
        "löschen",
        "entfernen",
        "elimina",
        "cancella",
        "削除",
        "消去",
        "删除",
        "移除",
    ),
    PUBLISH: (
        "publish",
        "post",
        "send",
        "share",
        "tweet",
        "reply",
        "comment",
        "broadcast",
        "release",
        "go live",
        "enviar",
        "publicar",
        "envoyer",
        "publier",
        "senden",
        "absenden",
        "veröffentlichen",
        "invia",
        "pubblica",
        "送信",
        "投稿",
        "公開",
        "发送",
        "发布",
    ),
    SUBMIT: ("submit", "save and submit", "enviar formulario", "soumettre", "einreichen", "提出"),
    CONFIRM: (
        "confirm",
        "apply",
        "approve",
        "accept",
        "agree",
        "i agree",
        "sign",
        "authorize",
        "allow",
        "grant",
        "continue to payment",
        "yes, delete",
        "confirmar",
        "aceptar",
        "confirmer",
        "accepter",
        "bestätigen",
        "akzeptieren",
        "確認",
        "同意",
        "确认",
    ),
    INSTALL: (
        "install",
        "update now",
        "download and install",
        "instalar",
        "installer",
        "installieren",
        "インストール",
        "安装",
    ),
    ACCOUNT_SECURITY: (
        "change password",
        "reset password",
        "two-factor",
        "2fa",
        "disable 2fa",
        "security settings",
        "log out of all",
        "sign out everywhere",
        "api key",
        "generate token",
        "new token",
        "add ssh key",
        "permissions",
        "make admin",
        "add member",
        "remove member",
        "transfer ownership",
    ),
}
_UI_PATTERNS = {
    category: re.compile(
        r"(?<![\w])(?:" + "|".join(re.escape(v) for v in sorted(words, key=len, reverse=True)) + r")(?![\w])"
    )
    for category, words in UI_VERBS.items()
}
_DANGEROUS_HREF = re.compile(
    r"/(?:delete|destroy|remove|checkout|pay|purchase|billing|logout|signout|unsubscribe|revoke|deactivate)\b", re.I
)
_DANGER_STYLE = re.compile(r"\b(?:danger|destructive|delete|critical|warning)\b", re.I)
_CARD_FIELD = re.compile(r"\b(?:card|cc-number|cc-csc|cvv|cvc|expir|iban)\b", re.I)


def _label_categories(label: str) -> list[str]:
    text = " ".join(label.lower().split())
    return [category for category, pattern in _UI_PATTERNS.items() if pattern.search(text)]


# --------------------------------------------------------------- command rules
_SQL_DESTRUCTIVE = re.compile(
    r"\b(?:drop\s+(?:table|database|schema|index|view|user|role|collection)|truncate(?:\s+table)?\s+\w|"
    r"alter\s+table\s+\S+\s+drop)\b",
    re.I,
)
_SQL_DELETE_OR_UPDATE = re.compile(r"\b(delete\s+from|update\s+[\w.\"`]+\s+set)\b(?P<rest>[^;]*)", re.I)
_DB_CLIENTS = frozenset(
    {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "mongosh", "mongo", "cockroach", "clickhouse-client"}
)
_PKG_INSTALLERS = {
    "pip": ("install", "uninstall"),
    "pip3": ("install", "uninstall"),
    "npm": ("install", "i", "uninstall", "remove", "rm", "add"),
    "pnpm": ("add", "install", "i", "remove", "rm"),
    "yarn": ("add", "remove", "global"),
    "bun": ("add", "install", "remove"),
    "brew": ("install", "uninstall", "reinstall", "remove", "upgrade"),
    "apt": ("install", "remove", "purge", "upgrade", "dist-upgrade"),
    "apt-get": ("install", "remove", "purge", "upgrade", "dist-upgrade"),
    "dnf": ("install", "remove", "upgrade"),
    "yum": ("install", "remove", "update"),
    "pacman": ("-S", "-R", "-Rs", "-Syu"),
    "gem": ("install", "uninstall"),
    "cargo": ("install", "uninstall"),
    "go": ("install",),
    "choco": ("install", "uninstall"),
    "winget": ("install", "uninstall"),
    "snap": ("install", "remove"),
}
_PUBLISH = {
    "npm": ("publish", "unpublish", "deprecate"),
    "pnpm": ("publish",),
    "yarn": ("publish", "npm"),
    "twine": ("upload",),
    "cargo": ("publish", "yank"),
    "poetry": ("publish",),
    "docker": ("push",),
    "podman": ("push",),
    "gem": ("push",),
    "flutter": ("pub",),
    "dart": ("pub",),
}
_CLOUD_DESTRUCTIVE_VERBS = re.compile(
    r"^(?:delete|destroy|terminate|remove|rm|purge|drop|deregister|release|detach|disable|stop-instances|"
    r"delete-\S+|terminate-\S+|remove-\S+|rb)$"
)
_SECURITY_OFF = (
    ("ufw", ("disable",)),
    ("setenforce", ("0",)),
    ("spctl", ("--master-disable",)),
    ("csrutil", ("disable",)),
    ("iptables", ("-F", "--flush")),
    ("systemctl", ("stop", "disable")),
    ("launchctl", ("unload", "bootout")),
)
_CREDENTIAL_CMDS = {
    "ssh-keygen": (),
    "ssh-add": (),
    "gpg": ("--gen-key", "--full-generate-key", "--delete-secret-keys"),
    "security": ("delete-generic-password", "delete-internet-password", "add-generic-password", "import"),
    "passwd": (),
    "htpasswd": (),
    "aws": ("configure",),
    "gcloud": ("auth",),
    "az": ("login", "ad"),
    "gh": ("auth", "secret", "ssh-key", "gpg-key"),
    "doctl": ("auth",),
    "heroku": ("auth", "authorizations"),
    "vault": ("write", "delete", "token"),
    "op": ("item", "signin"),
}
_REMOTE_HOSTS = re.compile(r"^[\w.-]+@[\w.-]+:|^[\w-]+(?:\.[\w-]+)+:")
_PROD_WORDS = re.compile(r"(?:^|[\s=:/_\-.])(prod|production|live|prd)(?:$|[\s/_\-.,;])", re.I)
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "nice", "ionice", "timeout", "xargs", "command", "exec"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "pwsh", "powershell", "cmd"})


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple-command token lists (best effort, never raises)."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|()"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _program(argv: list[str]) -> tuple[str, list[str], bool]:
    """Strip wrappers (``sudo``, ``VAR=x``, ``timeout 10`` …). Returns (program, args, elevated)."""
    elevated = False
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.rsplit("/", 1)[-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if name in _WRAPPERS:
            elevated = elevated or name in ("sudo", "doas")
            index += 1
            while index < len(argv) and (argv[index].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", argv[index])):
                index += 1
            continue
        return name, argv[index + 1 :], elevated
    return "", [], elevated


def classify_shell(command: str, verdict: SafetyVerdict, *, depth: int = 0) -> None:
    """Add semantic findings for a shell command to ``verdict``."""
    if depth > 3:
        verdict.add(REMOTE_CODE, "deeply nested shell invocation", RiskLevel.DANGEROUS)
        return
    if re.search(r"(?:curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", command, re.I):
        verdict.add(REMOTE_CODE, "downloads and executes remote code", RiskLevel.CRITICAL)
    if re.search(r"base64\s+(?:-d|--decode)[^|]*\|\s*(?:ba|z)?sh\b", command):
        verdict.add(REMOTE_CODE, "executes obfuscated (base64-decoded) code", RiskLevel.CRITICAL)
    if _SQL_DESTRUCTIVE.search(command) or _sql_unbounded(command):
        verdict.add(
            DATABASE_DESTRUCTIVE, "destructive SQL (DROP / TRUNCATE / unbounded DELETE or UPDATE)", RiskLevel.CRITICAL
        )
    if re.search(r"\b(?:flushall|flushdb)\b", command, re.I) or "dropDatabase" in command:
        verdict.add(DATABASE_DESTRUCTIVE, "wipes a datastore", RiskLevel.CRITICAL)
    if _PROD_WORDS.search(command):
        verdict.add(PRODUCTION, "targets a production environment", RiskLevel.DANGEROUS)
    for argv in _segments(command):
        program, args, elevated = _program(argv)
        if elevated:
            verdict.add(PRIVILEGE, "runs with elevated privileges (sudo)", RiskLevel.DANGEROUS)
        if not program:
            continue
        low = [a.lower() for a in args]
        if program in _SHELLS and any(a in ("-c", "/c", "-command") for a in low):
            flag = next(i for i, a in enumerate(low) if a in ("-c", "/c", "-command"))
            if flag + 1 < len(args):
                classify_shell(args[flag + 1], verdict, depth=depth + 1)
        if program in ("eval", "source", ".") and args:
            verdict.add(REMOTE_CODE, "evaluates dynamically constructed code", RiskLevel.DANGEROUS)
        if program == "rm" and any(a.startswith("-") and ("r" in a.lower() or a == "--recursive") for a in args):
            verdict.add(DESTRUCTIVE, "recursively deletes files", RiskLevel.DANGEROUS)
        elif program in ("rm", "unlink", "shred", "rmdir", "srm", "trash"):
            verdict.add(DESTRUCTIVE, "deletes files", RiskLevel.DANGEROUS)
        if program in ("mkfs", "fdisk", "diskutil", "parted", "wipefs") or program.startswith("mkfs."):
            verdict.add(DESTRUCTIVE, "modifies disks or partitions", RiskLevel.CRITICAL)
        if program == "dd" and any(a.startswith("of=/dev/") for a in args):
            verdict.add(DESTRUCTIVE, "writes raw data to a device", RiskLevel.CRITICAL)
        if program == "find" and ("-delete" in args or ("-exec" in args and "rm" in args)):
            verdict.add(DESTRUCTIVE, "deletes files found by find", RiskLevel.DANGEROUS)
        if program == "git":
            _classify_git(args, verdict)
        if program in _DB_CLIENTS or program in ("dropdb", "dropuser", "redis-cli", "mongorestore", "pg_restore"):
            if program in ("dropdb", "dropuser"):
                verdict.add(DATABASE_DESTRUCTIVE, f"`{program}` removes a database object", RiskLevel.CRITICAL)
            if program in ("mongorestore", "pg_restore") and any(a in ("--drop", "-c", "--clean") for a in args):
                verdict.add(DATABASE_DESTRUCTIVE, "restore replaces existing data", RiskLevel.CRITICAL)
        if program in _PKG_INSTALLERS and low:
            verbs = _PKG_INSTALLERS[program]
            sub = low[0]
            project_install = (
                sub in ("install", "i", "ci")
                and program in ("npm", "pnpm", "bun", "yarn")
                and not [a for a in args[1:] if not a.startswith("-")]
            )
            if (
                program in ("pip", "pip3")
                and sub == "install"
                and all(a.startswith("-") or a in (".", "-e") or a.startswith((".", "/")) for a in args[1:])
            ):
                project_install = True
            if sub in verbs and not project_install:
                if "uninstall" in sub or sub in ("remove", "rm", "purge", "-R", "-Rs"):
                    verdict.add(INSTALL, f"uninstalls software ({program} {sub})", RiskLevel.DANGEROUS)
                else:
                    verdict.add(INSTALL, f"installs software ({program} {sub})", RiskLevel.DANGEROUS)
            if "-g" in low or "--global" in low:
                verdict.add(INSTALL, "changes globally installed software", RiskLevel.DANGEROUS)
        if program == "gh":
            if low[:2] == ["repo", "delete"]:
                verdict.add(DESTRUCTIVE, "deletes a GitHub repository", RiskLevel.CRITICAL)
            elif (
                len(low) >= 2
                and low[0] in ("release", "pr", "issue", "repo", "gist")
                and low[1]
                in (
                    "create",
                    "delete",
                    "upload",
                    "merge",
                    "close",
                    "comment",
                    "edit",
                    "review",
                )
            ):
                verdict.add(PUBLISH, f"publishes to GitHub (gh {low[0]} {low[1]})", RiskLevel.DANGEROUS)
        elif program in _PUBLISH and low and low[0] in _PUBLISH[program]:
            verdict.add(PUBLISH, f"publishes or shares externally ({program} {' '.join(low[:2])})", RiskLevel.DANGEROUS)
        if (
            program in ("kubectl", "oc")
            and low
            and low[0] in ("delete", "drain", "cordon", "scale", "apply", "replace", "patch", "rollout")
        ):
            risk = RiskLevel.CRITICAL if low[0] in ("delete", "drain") else RiskLevel.DANGEROUS
            verdict.add(
                DESTRUCTIVE if low[0] in ("delete", "drain") else PRODUCTION,
                f"changes cluster resources (kubectl {low[0]})",
                risk,
            )
        if program == "helm" and low and low[0] in ("uninstall", "delete", "rollback"):
            verdict.add(DESTRUCTIVE, f"helm {low[0]}", RiskLevel.CRITICAL)
        if (
            program in ("terraform", "tofu", "pulumi")
            and low
            and low[0] in ("destroy", "apply", "up", "import", "state")
        ):
            risk = RiskLevel.CRITICAL if low[0] == "destroy" or "-auto-approve" in low else RiskLevel.DANGEROUS
            verdict.add(
                DESTRUCTIVE if low[0] == "destroy" else PRODUCTION, f"changes infrastructure ({program} {low[0]})", risk
            )
        if program in ("aws", "gcloud", "az", "doctl", "heroku", "flyctl", "vercel", "netlify") and any(
            _CLOUD_DESTRUCTIVE_VERBS.match(a) for a in low[:4]
        ):
            verdict.add(DESTRUCTIVE, f"deletes or stops cloud resources ({program})", RiskLevel.CRITICAL)
        if (
            program == "docker"
            and low
            and (
                low[0] in ("rm", "rmi", "volume", "system", "network")
                and any(a in ("rm", "prune", "-f", "--force", "-a") for a in low)
            )
        ):
            verdict.add(DESTRUCTIVE, "removes containers, images or volumes", RiskLevel.DANGEROUS)
        if program in _CREDENTIAL_CMDS:
            wanted = _CREDENTIAL_CMDS[program]
            if not wanted or (low and low[0] in wanted):
                verdict.add(CREDENTIAL, f"creates, changes or removes credentials ({program})", RiskLevel.DANGEROUS)
        if program == "aws" and low[:1] == ["iam"]:
            verdict.add(PERMISSION, "changes cloud identity and access", RiskLevel.CRITICAL)
        for name, verbs in _SECURITY_OFF:
            if program == name and any(v.lower() in low for v in verbs):
                verdict.add(SECURITY_CONTROL, f"disables or changes a security control ({program})", RiskLevel.CRITICAL)
        if program in ("chmod", "chown", "chgrp", "icacls", "setfacl"):
            if any(a in ("-r", "-R", "--recursive") for a in args) or any(a in ("777", "a+rwx", "o+w") for a in args):
                verdict.add(PERMISSION, f"changes file permissions broadly ({program})", RiskLevel.DANGEROUS)
        if program in ("scp", "rsync", "sftp") and any(_REMOTE_HOSTS.match(a) for a in args):
            verdict.add(DATA_EGRESS, "copies data to or from a remote host", RiskLevel.DANGEROUS)
        if program in ("curl", "wget", "http", "https") and _sends_body(program, args) and not _local_only(args):
            verdict.add(DATA_EGRESS, "sends local data to a remote host", RiskLevel.DANGEROUS)
        if _SECRET_READ.search(" ".join(args)):
            verdict.add(CREDENTIAL, "reads private keys or credential files", RiskLevel.DANGEROUS)
        if program in ("shutdown", "reboot", "halt", "poweroff"):
            verdict.add(DESTRUCTIVE, f"stops the machine ({program})", RiskLevel.CRITICAL)
        if program in ("killall", "pkill") or (
            program == "kill" and any(a in ("-9", "-KILL", "-SIGKILL") for a in args)
        ):
            verdict.add(DESTRUCTIVE, f"forcibly stops processes ({program})", RiskLevel.DANGEROUS)


_BODY_FLAGS = (
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json",
    "-F", "--form", "--form-string", "-T", "--upload-file",
    "--post-data", "--post-file", "--body-data", "--body-file",
)  # fmt: skip
_LOCAL_URL = re.compile(r"^(?:https?://)?(?:localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0)(?:[:/]|$)", re.I)
_URL_ARG = re.compile(
    r"^(?:https?://|[\w-]+(?:\.[\w-]+)+(?::\d+)?(?:/|$)|localhost\b|\[::1\]|\d+\.\d+\.\d+\.\d+)", re.I
)
_SECRET_READ = re.compile(
    r"(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|/root)?/?\.ssh/(?:id_[\w.-]+|[\w.-]*key[\w.-]*)"
    r"|\.aws/credentials|\.netrc\b|\.docker/config\.json|\.kube/config|\.git-credentials"
    r"|(?:^|[\s\"'=@(/])\.env(?!\.(?:example|sample|template|dist|defaults?)\b)(?:\.[\w-]+)?\b",
    re.I,
)


def _sends_body(program: str, args: list[str]) -> bool:
    """curl / wget / HTTPie arguments that put local data in the request."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in _BODY_FLAGS or (arg.startswith("-d") and len(arg) > 2 and not arg.startswith("--")):
            return True
        if arg.startswith("@") and len(arg) > 1:
            return True
    if program in ("http", "https"):  # HTTPie: `http POST host field=value`, `field:=json`, `field@file`
        positional = [a for a in args if not a.startswith("-")]
        return any(re.match(r"^[\w.-]+(?:=|:=|@)", a) and not _URL_ARG.match(a) for a in positional[1:])
    return False


def _local_only(args: list[str]) -> bool:
    """True when every URL/host argument is this machine (local development calls)."""
    targets = [a for a in args if not a.startswith("-") and _URL_ARG.match(a)]
    return bool(targets) and all(_LOCAL_URL.match(t) for t in targets)


def _sql_unbounded(command: str) -> bool:
    for match in _SQL_DELETE_OR_UPDATE.finditer(command):
        if not re.search(r"\bwhere\b", match.group("rest"), re.I):
            return True
    return False


def _classify_git(args: list[str], verdict: SafetyVerdict) -> None:
    low = [a.lower() for a in args]
    sub = next((a for a in low if not a.startswith("-")), "")
    if sub == "push":
        if any(
            a in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d", "--prune")
            or a.startswith("--force")
            for a in low
        ) or any(a.startswith("+") or a.startswith(":") for a in args):
            verdict.add(DESTRUCTIVE, "rewrites or deletes remote history (force push / delete)", RiskLevel.CRITICAL)
        verdict.add(PUBLISH, "pushes commits to a shared remote", RiskLevel.DANGEROUS)
    elif sub == "reset" and "--hard" in low:
        verdict.add(DESTRUCTIVE, "discards uncommitted work (git reset --hard)", RiskLevel.DANGEROUS)
    elif sub == "clean" and any(a.startswith("-") and "f" in a for a in low):
        verdict.add(DESTRUCTIVE, "deletes untracked files (git clean -f)", RiskLevel.DANGEROUS)
    elif sub in ("checkout", "restore") and ("." in args or ("--" in args and args[-1] == ".")):
        verdict.add(DESTRUCTIVE, "discards local changes", RiskLevel.DANGEROUS)
    elif sub == "branch" and any(a in ("-d", "-D", "--delete") for a in args):
        verdict.add(DESTRUCTIVE, "deletes a branch", RiskLevel.DANGEROUS)
    elif sub in ("filter-branch", "filter-repo") or (sub == "gc" and "--prune=now" in low):
        verdict.add(DESTRUCTIVE, f"rewrites repository history (git {sub})", RiskLevel.CRITICAL)
    elif sub == "tag" and any(a in ("-d", "--delete") for a in args):
        verdict.add(DESTRUCTIVE, "deletes a tag", RiskLevel.DANGEROUS)
    elif sub == "stash" and any(a in ("drop", "clear") for a in low):
        verdict.add(DESTRUCTIVE, "drops stashed work", RiskLevel.DANGEROUS)
    if sub in ("commit", "push") and "--no-verify" in low:
        verdict.add(SECURITY_CONTROL, "skips git hooks (--no-verify)", RiskLevel.DANGEROUS)
    if sub == "config" and any("credential" in a for a in low):
        verdict.add(CREDENTIAL, "changes git credential configuration", RiskLevel.DANGEROUS)


# ----------------------------------------------------------------- file rules
SECURITY_CONTROL_PATHS = (
    ".highhx/policies.yaml",
    ".highhx/config.yaml",
    ".highhx/profiles/*",
    ".highhx/hooks/*",
    ".git/hooks/*",
    ".github/workflows/*",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    ".husky/*",
    "Jenkinsfile",
    ".circleci/*",
    "**/.htaccess",
    "**/sudoers*",
)


def _is_security_control(path: str) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
        for pattern in SECURITY_CONTROL_PATHS
    )


# -------------------------------------------------------------------- policy
class SafetyPolicy:
    """Classifies actions. Pure and deterministic: same input, same verdict."""

    def classify(self, action: ActionDescriptor) -> SafetyVerdict:
        verdict = SafetyVerdict(RiskLevel.SAFE)
        kind = action.kind
        if action.environment and _PROD_WORDS.search(f" {action.environment} "):
            verdict.add(PRODUCTION, f"affects the {action.environment} environment", RiskLevel.CRITICAL)
        if kind == ActionKind.READ:
            return verdict
        if kind == ActionKind.EXEC and action.command:
            base = classify_command(action.command)
            verdict.risk = max(verdict.risk, base.risk)
            verdict.reasons.extend(r for r in base.reasons if r not in verdict.reasons)
            if not base.bypassable:
                verdict.blocked = True
                verdict.add(DESTRUCTIVE, "matches a non-bypassable HighhX rule", RiskLevel.CRITICAL)
            classify_shell(action.command, verdict)
            verdict.risk = max(verdict.risk, RiskLevel.NORMAL)
        elif kind == ActionKind.GIT and action.command:
            verdict.risk = RiskLevel.NORMAL
            classify_shell(action.command, verdict)
        elif kind == ActionKind.WRITE_FILE:
            verdict.risk = RiskLevel.NORMAL
            if _is_security_control(action.target):
                verdict.add(
                    SECURITY_CONTROL, f"{action.target} controls policies, approvals, CI or hooks", RiskLevel.DANGEROUS
                )
        elif kind == ActionKind.DELETE_FILE:
            verdict.add(DESTRUCTIVE, f"deletes {action.target}", RiskLevel.DANGEROUS)
            if _is_security_control(action.target):
                verdict.add(
                    SECURITY_CONTROL, f"{action.target} controls policies, approvals, CI or hooks", RiskLevel.CRITICAL
                )
        elif kind in (ActionKind.DEPLOY, ActionKind.ROLLBACK):
            production = action.attr("production") == "True" or bool(_PROD_WORDS.search(f" {action.target} "))
            if production:
                verdict.add(PRODUCTION, "production deployment", RiskLevel.CRITICAL)
            else:
                verdict.add(PUBLISH, "deploys to a shared environment", RiskLevel.DANGEROUS)
        elif kind in (ActionKind.UI_CLICK, ActionKind.UI_KEY, ActionKind.UI_SELECT):
            self._classify_ui(action, verdict)
        elif kind == ActionKind.UI_TYPE:
            verdict.risk = RiskLevel.NORMAL
            input_type = action.attr("input_type").lower()
            autocomplete = action.attr("autocomplete").lower()
            if input_type == "password" or "password" in autocomplete or "one-time-code" in autocomplete:
                verdict.add(CREDENTIAL, "enters a password or one-time code", RiskLevel.DANGEROUS)
                if action.actor == Actor.AGENT:
                    verdict.agent_blocked = True
            if _CARD_FIELD.search(f"{action.attr('name')} {autocomplete} {action.target}"):
                verdict.add(FINANCIAL, "enters payment card details", RiskLevel.CRITICAL)
                if action.actor == Actor.AGENT:
                    verdict.agent_blocked = True
        elif kind == ActionKind.NAVIGATE:
            verdict.risk = RiskLevel.NORMAL
            scheme = action.target.split(":", 1)[0].lower()
            if scheme in ("file", "javascript", "chrome", "about") and not action.target.startswith("about:blank"):
                verdict.add(SECURITY_CONTROL, f"navigates to a privileged URL scheme ({scheme}:)", RiskLevel.DANGEROUS)
            if _DANGEROUS_HREF.search(action.target):
                verdict.add(CONFIRM, "the URL performs a sensitive account or payment action", RiskLevel.DANGEROUS)
        elif kind == ActionKind.APP_LAUNCH:
            verdict.risk = RiskLevel.NORMAL
        elif kind == ActionKind.UI_SCROLL:
            verdict.risk = RiskLevel.SAFE
        return verdict

    def _classify_ui(self, action: ActionDescriptor, verdict: SafetyVerdict) -> None:
        verdict.risk = RiskLevel.NORMAL
        label = " ".join(
            x for x in (action.attr("name"), action.attr("value"), action.attr("title"), action.target) if x
        )
        for category in _label_categories(label):
            verdict.add(
                category, f'the control is labelled "{action.attr("name") or action.target}"', RiskLevel.DANGEROUS
            )
        role = action.attr("role").lower()
        element_type = action.attr("type").lower()
        if (
            action.kind == ActionKind.UI_KEY
            and action.attr("key").lower() in ("enter", "return")
            and action.attr("in_form") == "True"
        ):
            verdict.add(SUBMIT, "pressing Enter submits the form", RiskLevel.DANGEROUS)
        if element_type == "submit" or (role == "button" and action.attr("form_method").lower() == "post"):
            verdict.add(SUBMIT, "the control submits a form", RiskLevel.DANGEROUS)
        if action.attr("form_has_password") == "True" and (
            element_type == "submit" or action.kind == ActionKind.UI_KEY
        ):
            verdict.add(ACCOUNT_SECURITY, "submits credentials", RiskLevel.DANGEROUS)
        if _DANGER_STYLE.search(action.attr("class")):
            verdict.add(DESTRUCTIVE, "the control is styled as destructive", RiskLevel.DANGEROUS)
        href = action.attr("href")
        if href and _DANGEROUS_HREF.search(href):
            verdict.add(CONFIRM, "the link performs a sensitive account or payment action", RiskLevel.DANGEROUS)
        if action.attr("download") == "True":
            verdict.add(INSTALL, "downloads a file", RiskLevel.DANGEROUS)
        if FINANCIAL in verdict.categories:
            verdict.risk = RiskLevel.CRITICAL
