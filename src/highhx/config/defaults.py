"""Default values and well-known file names."""

from __future__ import annotations

CONFIG_DIR = ".highhx"
CONFIG_FILE = "config.yaml"
ENVIRONMENT_FILE = "environment.yaml"
POLICIES_FILE = "policies.yaml"
WORKFLOWS_DIR = "workflows"
HOOKS_DIR = "hooks"
PROFILES_DIR = "profiles"
PLUGINS_DIR = "plugins"
PLUGINS_LOCK = "plugins.lock.json"
STATE_DIR = "state"
LOGS_DIR = "logs"
BACKUPS_DIR = "backups"
DB_FILE = "highhx.db"

CONFIG_VERSION = 1

COMMAND_NAMES = (
    "dev",
    "start",
    "test",
    "build",
    "lint",
    "format",
    "fix",
    "typecheck",
    "install",
    "update",
    "clean",
    "package",
    "check",
    "coverage",
    "run",
)

DEPLOY_TYPES = ("local", "docker", "ssh", "kubernetes", "terraform")

DEFAULT_DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_TAG_PREFIX = "v"
DEFAULT_CHANGELOG = "CHANGELOG.md"
DEFAULT_ENV_PROFILE = "development"

GITIGNORE_ENTRIES = (".highhx/state/", ".highhx/logs/", ".highhx/backups/", ".highhx/cache/")
