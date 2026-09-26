"""``highhx diagnose``: identify concrete problems (and whether they can be repaired)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.config.defaults import GITIGNORE_ENTRIES
from highhx.core.errors import HighhXError
from highhx.execution.command import CommandSpec
from highhx.execution.shell import needs_shell
from highhx.utils.platform import supports_posix_permissions
from highhx.utils.processes import which


@dataclass
class Diagnosis:
    id: str
    severity: str
    problem: str
    detail: str = ""
    fix: str = ""
    repair: str | None = None
    """Identifier of an automatic repair (see highhx.diagnostics.repair)."""
    repair_risk: RiskLevel = RiskLevel.SAFE

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "problem": self.problem,
            "detail": self.detail,
            "fix": self.fix,
            "repairable": self.repair is not None,
            "repair": self.repair,
            "repair_risk": self.repair_risk.label,
        }


def diagnose(app: Any) -> list[Diagnosis]:
    found: list[Diagnosis] = []
    paths = app.paths
    if not app.initialized:
        found.append(
            Diagnosis("not-initialized", "error", "HighhX is not initialized in this project", fix="Run `highhx init`.")
        )
        return found + _git(app)
    try:
        app.load_config()
    except HighhXError as exc:
        found.append(
            Diagnosis(
                "config-invalid",
                "error",
                exc.message,
                "; ".join(exc.details[:5]),
                "Fix .highhx/config.yaml (see `highhx config validate`).",
            )
        )
    for directory in (paths.workflows_dir, paths.hooks_dir, paths.state_dir, paths.logs_dir):
        if not directory.is_dir():
            found.append(
                Diagnosis(
                    f"missing-dir:{directory.name}",
                    "warning",
                    f".highhx/{directory.name}/ is missing",
                    repair="create-dirs",
                    fix="highhx repair",
                )
            )
    if not app.workflow_loader.keys():
        found.append(
            Diagnosis(
                "no-workflows",
                "warning",
                "No workflows defined",
                fix="highhx repair (restores default workflows)",
                repair="default-workflows",
                repair_risk=RiskLevel.NORMAL,
            )
        )
    from highhx.workflows.validator import validate_file

    for ref in app.workflow_loader.list():
        report = validate_file(ref.path, loader=app.workflow_loader, check_tools=False, base_dir=app.root)
        if not report.ok:
            found.append(
                Diagnosis(
                    f"workflow-invalid:{ref.key}",
                    "error",
                    f"Workflow '{ref.key}' is invalid",
                    "; ".join(report.errors[:3]),
                    f"Edit {ref.path.name} (see `highhx workflow validate {ref.key}`).",
                )
            )
    for kind, command in sorted(app.commands().items()):
        program = CommandSpec(command).program()
        if (
            program
            and not needs_shell(program)
            and not program.startswith(("./", ".\\"))
            and program != "highhx"
            and which(program) is None
        ):
            found.append(
                Diagnosis(
                    f"missing-tool:{kind}",
                    "error",
                    f"`{program}` (used by commands.{kind}) is not installed",
                    fix=f"Install {program} or change commands.{kind}.",
                )
            )
    try:
        for check in app.environment.check():
            if str(check.status) == "fail":
                found.append(
                    Diagnosis(
                        f"env:{check.name}",
                        "error",
                        f"{check.name}: {check.message}",
                        f"profile {app.environment.active_profile()}",
                        check.hint or "",
                    )
                )
    except HighhXError as exc:
        found.append(Diagnosis("env-config-invalid", "error", exc.message, fix="Fix .highhx/environment.yaml."))
    if supports_posix_permissions():
        for env_file in sorted(app.root.glob(".env*")):
            if (
                env_file.is_file()
                and env_file.name not in (".env.example", ".env.sample")
                and env_file.stat().st_mode & 0o077
            ):
                found.append(
                    Diagnosis(
                        f"env-perms:{env_file.name}",
                        "warning",
                        f"{env_file.name} is readable by other users",
                        repair="env-permissions",
                        repair_risk=RiskLevel.NORMAL,
                        fix=f"chmod 600 {env_file.name}",
                    )
                )
    if app.db is None and app.paths.db_file.exists():
        found.append(
            Diagnosis(
                "state-db-unreadable",
                "error",
                "The HighhX state database cannot be opened",
                app.db_error or "",
                "highhx repair (moves it aside and starts a fresh one)",
                repair="reset-state-db",
                repair_risk=RiskLevel.NORMAL,
            )
        )
    stale = app.service_registry.stale_states()
    if stale:
        found.append(
            Diagnosis(
                "stale-services",
                "warning",
                f"{len(stale)} stale service state file(s)",
                ", ".join(s.name for s in stale),
                repair="stale-services",
            )
        )
    history = app.history
    if history is not None:
        running = history.list(limit=50, status="running")
        from highhx.utils.processes import pid_alive

        dead = [r for r in running if not pid_alive(int(r.metadata.get("pid", -1)))]
        if dead:
            found.append(
                Diagnosis(
                    "stale-executions",
                    "info",
                    f"{len(dead)} execution(s) still marked running",
                    repair="stale-executions",
                )
            )
        for record in history.list(limit=3, status="failed"):
            found.append(
                Diagnosis(
                    f"recent-failure:{record.id}",
                    "info",
                    f"Recent failure: {record.kind} {record.name}",
                    record.error or "",
                    f"highhx logs {record.id}",
                )
            )
    if app.config.services:
        from highhx.services.ports import is_port_free

        running = {s.name for s in app.services.status() if s.running}
        for name, service in app.config.services.items():
            if service.port and name not in running and not is_port_free(service.port):
                found.append(
                    Diagnosis(
                        f"port-conflict:{name}",
                        "error",
                        f"Port {service.port} for service '{name}' is taken by another process",
                        fix="`highhx ports` shows which process owns it.",
                    )
                )
    return found + _git(app)


def _git(app: Any) -> list[Diagnosis]:
    found: list[Diagnosis] = []
    root = app.root
    if (root / ".git").exists():
        gitignore = root / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8").split() if gitignore.exists() else []
        missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
        if app.initialized and missing:
            found.append(
                Diagnosis(
                    "gitignore",
                    "warning",
                    "HighhX state directories are not git-ignored",
                    ", ".join(missing),
                    repair="gitignore",
                    fix="highhx repair",
                )
            )
    return found
