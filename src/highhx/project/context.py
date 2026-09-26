"""Well-known paths of a HighhX project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from highhx.config import defaults as d


@dataclass(frozen=True)
class ProjectPaths:
    """All paths HighhX uses inside a project."""

    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / d.CONFIG_DIR

    @property
    def config_file(self) -> Path:
        return self.config_dir / d.CONFIG_FILE

    @property
    def environment_file(self) -> Path:
        return self.config_dir / d.ENVIRONMENT_FILE

    @property
    def policies_file(self) -> Path:
        return self.config_dir / d.POLICIES_FILE

    @property
    def workflows_dir(self) -> Path:
        return self.config_dir / d.WORKFLOWS_DIR

    @property
    def hooks_dir(self) -> Path:
        return self.config_dir / d.HOOKS_DIR

    @property
    def profiles_dir(self) -> Path:
        return self.config_dir / d.PROFILES_DIR

    @property
    def plugins_dir(self) -> Path:
        return self.config_dir / d.PLUGINS_DIR

    @property
    def plugins_lock(self) -> Path:
        return self.config_dir / d.PLUGINS_LOCK

    @property
    def state_dir(self) -> Path:
        return self.config_dir / d.STATE_DIR

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / d.LOGS_DIR

    @property
    def backups_dir(self) -> Path:
        return self.config_dir / d.BACKUPS_DIR

    @property
    def db_file(self) -> Path:
        return self.state_dir / d.DB_FILE

    @property
    def services_state_dir(self) -> Path:
        return self.state_dir / "services"

    @property
    def services_logs_dir(self) -> Path:
        return self.logs_dir / "services"

    @property
    def initialized(self) -> bool:
        return self.config_file.is_file()
