"""What the agent may touch, and who approves it.

The agent adds no execution path of its own. Every action a tool takes is
described as an :class:`~highhx.safety.actions.ActionDescriptor` and passes the
shared :class:`~highhx.safety.gate.ActionGate` — deterministic classification,
project policy (``agent:*`` actions in ``policies.yaml``), the approval mode,
human confirmation bound to the exact action, and the audit trail — before it
runs through the HighhX engine. On top of that this module confines paths: the
agent only sees and changes files inside the project root, never secret files,
VCS internals or HighhX state.
"""

from __future__ import annotations

import contextlib
import fnmatch
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from highhx.agent.tools.base import ToolError
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs
from highhx.safety.audit import AuditEvent, AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode, Authorization, GatePrompter

if TYPE_CHECKING:
    from highhx.commands import App

__all__ = ["AgentPermissions", "ApprovalMode"]

# Files the agent may never read or write: their contents would leave the machine.
SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
)
SECRET_FILE_EXCEPTIONS = (".env.example", ".env.sample", ".env.template")
SECRET_NAME_HINTS = ("SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY", "API_KEY", "APIKEY")
DATA_SUFFIXES = (".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties")
# Directories the agent may read but never write.
READ_ONLY_DIRS = (".git", ".highhx/state", ".highhx/logs", ".highhx/backups")
# Directories never listed or read (noise or machine state).
HIDDEN_DIRS = (".git", ".highhx/state", ".highhx/logs", ".highhx/backups")


class AgentPermissions:
    def __init__(
        self,
        app: App,
        prompter: GatePrompter,
        *,
        mode: ApprovalMode = ApprovalMode.ASK,
        assume_yes: bool = False,
        audit: AuditLog | None = None,
        session_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        self.app = app
        self.prompter = prompter
        self.gate = ActionGate(
            app.engine,
            prompter,
            source="agent",
            mode=mode,
            assume_yes=assume_yes,
            audit=audit,
            session_id=session_id,
            account_id=account_id,
        )

    @property
    def mode(self) -> ApprovalMode:
        return self.gate.mode

    @mode.setter
    def mode(self, value: ApprovalMode) -> None:
        self.gate.mode = value

    @property
    def grants(self) -> set[str]:
        return self.gate.grants

    @property
    def root(self) -> Path:
        return self.app.root

    # ------------------------------------------------------------------ paths
    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root.resolve()).as_posix() or "."
        except ValueError:
            return path.as_posix()

    def resolve(self, raw: str, *, write: bool = False, must_exist: bool = False) -> Path:
        """Resolve a model-supplied path and enforce confinement. Raises :class:`ToolError`."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path must be a non-empty string")
        if "\x00" in raw:
            raise ToolError("path contains a NUL byte")
        root = self.root.resolve()
        candidate = Path(raw.strip()).expanduser()
        target = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if target != root and not target.is_relative_to(root):
            raise ToolError(f"{raw} is outside the project ({root}); only project files are accessible")
        rel = self.relative(target)
        if self.is_secret(rel):
            raise ToolError(
                f"{rel} may contain secrets; HighhX never sends secret files to the AI model. "
                "Ask the user to make that change themselves."
            )
        if any(rel == d or rel.startswith(d + "/") for d in HIDDEN_DIRS) and not write:
            raise ToolError(f"{rel} is internal state and is not readable by the agent")
        if write:
            if rel == ".":
                raise ToolError("cannot write the project root itself")
            if any(rel == d or rel.startswith(d + "/") for d in READ_ONLY_DIRS):
                raise ToolError(f"{rel} is protected; the agent cannot modify it")
            forbidden = self.app.policy.forbidden_matches([rel])
            if forbidden:
                raise ToolError(f"{rel} matches forbidden_files in .highhx/policies.yaml")
        if must_exist and not target.exists():
            raise ToolError(f"{rel} does not exist")
        return target

    @staticmethod
    def is_secret(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if name in SECRET_FILE_EXCEPTIONS:
            return False
        if any(fnmatch.fnmatch(name, p) for p in SECRET_FILE_PATTERNS):
            return True
        # Data files named like secrets (`secrets.yaml`, `db_password.txt`) — not source code about them.
        stem, _, suffix = name.partition(".")
        return any(hint in stem.upper() for hint in SECRET_NAME_HINTS) and (
            not suffix or f".{suffix.rsplit('.', 1)[-1]}" in DATA_SUFFIXES
        )

    # -------------------------------------------------------------- approvals
    def action(
        self,
        kind: ActionKind,
        summary: str,
        *,
        tool: str,
        target: str = "",
        command: str | None = None,
        application: str | None = None,
        environment: str | None = None,
        **attributes: object,
    ) -> ActionDescriptor:
        """Describe an action the agent wants to take."""
        return ActionDescriptor(
            kind=kind,
            summary=summary,
            tool=tool,
            target=target,
            application=application if application is not None else f"project {self.app.root.name}",
            command=command,
            environment=environment,
            actor=Actor.AGENT,
            attributes=attrs(**attributes),
        )

    def authorize(
        self,
        action: ActionDescriptor,
        *,
        policy_action: str,
        grant: str | None = None,
        details: Sequence[str] = (),
        engine_prompts: bool = False,
        always_confirm: bool = False,
    ) -> Authorization:
        return self.gate.authorize(
            action,
            policy_action=policy_action,
            grant=grant,
            details=details,
            engine_prompts=engine_prompts,
            always_confirm=always_confirm,
        )

    @contextlib.contextmanager
    def executing(self, authorization: Authorization, current: ActionDescriptor | None = None) -> Iterator[AuditEvent]:
        with self.gate.executing(authorization, current) as event:
            yield event
