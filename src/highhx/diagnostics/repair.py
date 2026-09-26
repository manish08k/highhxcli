"""``highhx repair``: safe, deterministic repairs only."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Any

from highhx.config.defaults import GITIGNORE_ENTRIES
from highhx.diagnostics.diagnose import Diagnosis
from highhx.utils.filesystem import atomic_write_text, ensure_dir, ensure_gitignore_entries

REPAIR_DESCRIPTIONS = {
    "create-dirs": "Create missing .highhx directories",
    "gitignore": "Add HighhX state directories to .gitignore",
    "stale-services": "Remove state files of services that are no longer running",
    "stale-executions": "Mark executions left 'running' by crashed processes as cancelled",
    "env-permissions": "Restrict .env files to the owner (chmod 600)",
    "default-workflows": "Restore the default workflows from templates",
    "reset-state-db": "Move the unreadable state database aside and create a new one",
}


@dataclass
class RepairResult:
    repair: str
    description: str
    applied: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _apply(app: Any, repair: str) -> str:
    paths = app.paths
    if repair == "create-dirs":
        for directory in (paths.workflows_dir, paths.hooks_dir, paths.state_dir, paths.logs_dir):
            ensure_dir(directory)
        return "directories created"
    if repair == "gitignore":
        added = ensure_gitignore_entries(app.root / ".gitignore", GITIGNORE_ENTRIES)
        return f"added {', '.join(added) or 'nothing'}"
    if repair == "stale-services":
        stale = app.service_registry.stale_states()
        for state in stale:
            app.service_registry.clear_state(state.name)
        return f"removed {len(stale)} state file(s)"
    if repair == "stale-executions":
        return f"updated {app.history.mark_stale_running()} execution(s)"
    if repair == "env-permissions":
        fixed = []
        for env_file in sorted(app.root.glob(".env*")):
            if (
                env_file.is_file()
                and env_file.name not in (".env.example", ".env.sample")
                and env_file.stat().st_mode & 0o077
            ):
                os.chmod(env_file, stat.S_IRUSR | stat.S_IWUSR)
                fixed.append(env_file.name)
        return f"chmod 600 {', '.join(fixed)}"
    if repair == "default-workflows":
        from highhx.project.initializer import plan_init

        plan = plan_init(app.profile, commands=app.commands())
        written = []
        for rel, content in plan.files.items():
            if rel.startswith(".highhx/workflows/") and not (app.root / rel).exists():
                atomic_write_text(app.root / rel, content)
                written.append(rel.rsplit("/", 1)[-1])
        return f"restored {', '.join(written) or 'nothing'}"
    if repair == "reset-state-db":
        from highhx.storage.database import Database
        from highhx.utils.time import utc_now

        source = paths.db_file
        target = source.with_name(f"{source.name}.corrupt-{utc_now().strftime('%Y%m%dT%H%M%S')}")
        source.replace(target)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                sidecar.replace(target.with_name(target.name + suffix))
        Database.open(source).close()
        return f"kept the old file as {target.name}"
    raise ValueError(f"unknown repair {repair}")


def run_repairs(app: Any, diagnoses: list[Diagnosis]) -> list[RepairResult]:
    results: list[RepairResult] = []
    seen: set[str] = set()
    for diagnosis in diagnoses:
        repair = diagnosis.repair
        if repair is None or repair in seen:
            continue
        seen.add(repair)
        description = REPAIR_DESCRIPTIONS[repair]
        app.engine.approve(
            description, diagnosis.repair_risk, details=[diagnosis.problem], policy_action=f"repair:{repair}"
        )
        if app.engine.dry_run:
            results.append(RepairResult(repair, description, False, "dry run"))
            continue
        results.append(RepairResult(repair, description, True, _apply(app, repair)))
    return results
