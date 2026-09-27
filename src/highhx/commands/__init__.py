"""CLI commands and the application container (composition root).

Command modules stay thin: they parse arguments, call a domain service obtained
from :class:`App`, and render the result through :class:`highhx.ui.output.Output`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from highhx.approvals.manager import ApprovalManager
from highhx.approvals.policy import ApprovalPolicy
from highhx.config.loader import LoadedConfig, load_config
from highhx.config.schema import HighhXConfig
from highhx.core.context import ExecutionContext, Options
from highhx.core.engine import Engine
from highhx.core.errors import ConfigError, HighhXError, ProjectNotInitializedError
from highhx.observability.tracing import Tracer
from highhx.policy.engine import PolicyEngine, PolicySet
from highhx.project.context import ProjectPaths
from highhx.project.detector import ProjectProfile, detect_project
from highhx.project.workspace import resolve_root
from highhx.security.secrets import Redactor
from highhx.storage.cache import StateStore
from highhx.storage.database import Database
from highhx.storage.history import HistoryStore
from highhx.storage.logs import LogStore
from highhx.ui.output import Output
from highhx.ui.prompts import ConsolePrompter
from highhx.utils.paths import user_data_dir

if TYPE_CHECKING:
    from highhx.building.builder import Builder
    from highhx.cloud.account import CloudAccount
    from highhx.database.manager import DatabaseManager
    from highhx.dependencies.manager import DependencyManager
    from highhx.deployment.manager import DeploymentManager
    from highhx.environment.manager import EnvironmentManager
    from highhx.git.manager import GitManager
    from highhx.git.repository import GitRepository
    from highhx.integrations.docker import DockerClient
    from highhx.plugins.manager import PluginManager
    from highhx.plugins.registry import PluginRegistry
    from highhx.plugins.sandbox import TrustStore
    from highhx.release.manager import ReleaseManager
    from highhx.services.manager import ServiceManager
    from highhx.services.registry import ServiceRegistry
    from highhx.testing.runner import TestRunner
    from highhx.workflows.engine import WorkflowEngine
    from highhx.workflows.loader import WorkflowLoader


class App:
    """Lazily constructed services for one CLI invocation (no global state)."""

    def __init__(self, options: Options | None = None, cwd: Path | None = None) -> None:
        self.options = options or Options()
        self.start_dir = (cwd or Path.cwd()).resolve()
        self.ctx = ExecutionContext(options=self.options, cwd=self.start_dir)
        self._closers: list[Callable[[], None]] = []
        self._config_error: HighhXError | None = None
        self.db_error: str | None = None

    # ------------------------------------------------------------------ basics
    def set_cwd(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise click.BadParameter(f"{path} is not a directory", param_hint="--cwd")
        self.start_dir = resolved
        self.ctx.cwd = resolved

    @cached_property
    def output(self) -> Output:
        from highhx.observability.logger import setup_logging

        setup_logging(
            verbose=self.options.verbose,
            debug=self.options.debug,
            log_file=self.paths.logs_dir / "highhx-debug.log" if self.options.debug and self.initialized else None,
            redactor=self.redactor,
        )
        return Output(
            json_mode=self.options.json,
            quiet=self.options.quiet,
            verbose=self.options.verbose or self.options.debug,
            no_color=self.options.no_color,
            redactor=self.redactor,
        )

    @cached_property
    def _root_info(self) -> tuple[Path, bool]:
        return resolve_root(self.start_dir)

    @property
    def root(self) -> Path:
        return self._root_info[0]

    @property
    def initialized(self) -> bool:
        return self._root_info[1]

    @cached_property
    def paths(self) -> ProjectPaths:
        return ProjectPaths(self.root)

    def require_project(self) -> None:
        if not self.initialized:
            raise ProjectNotInitializedError()

    # ------------------------------------------------------------------ config
    @cached_property
    def loaded_config(self) -> LoadedConfig | None:
        if not self.initialized:
            return None
        try:
            return load_config(self.root, profile=self.options.profile)
        except ConfigError as exc:
            self._config_error = exc
            return None

    def load_config(self) -> HighhXConfig:
        """Config, raising if it is invalid."""
        if self.initialized and self.loaded_config is None and self._config_error is not None:
            raise self._config_error
        return self.config

    @property
    def config(self) -> HighhXConfig:
        loaded = self.loaded_config
        return loaded.config if loaded else HighhXConfig()

    @cached_property
    def policy(self) -> PolicyEngine:
        return PolicyEngine(PolicySet.load(self.paths.policies_file)) if self.initialized else PolicyEngine()

    # ----------------------------------------------------------------- project
    @cached_property
    def profile(self) -> ProjectProfile:
        profile = detect_project(self.root)
        for plugin, detector in self.plugins.detectors:
            try:
                extra = detector(self.root)
            except Exception as exc:
                self.output.warn(f"plugin {plugin} detector failed: {exc}")
                continue
            for detection in extra:
                bucket = {
                    "language": profile.languages,
                    "framework": profile.frameworks,
                    "package_manager": profile.package_managers,
                    "database": profile.databases,
                    "container": profile.containers,
                }.get(detection.kind, profile.languages)
                bucket.append(detection)
                if detection.kind == "language" and detection.name not in profile.stacks:
                    profile.stacks.append(detection.name)
        return profile

    def commands(self) -> dict[str, str]:
        """Effective commands: configured values override detected ones."""
        merged = dict(self.profile.commands)
        merged.update(self.config.commands)
        return merged

    # ----------------------------------------------------------------- storage
    @cached_property
    def db(self) -> Database | None:
        path = self.paths.db_file if self.initialized else user_data_dir() / "history.db"
        try:
            db = Database.open(path)
        except Exception as exc:  # storage problems must not block commands
            self.db_error = str(exc)
            self.output.warn(f"history storage unavailable ({exc}); continuing without history — see `highhx diagnose`")
            return None
        self._closers.append(db.close)
        return db

    @cached_property
    def redactor(self) -> Redactor:
        return Redactor()

    @cached_property
    def history(self) -> HistoryStore | None:
        return HistoryStore(self.db, self.redactor) if self.db else None

    @cached_property
    def logs(self) -> LogStore:
        directory = self.paths.logs_dir if self.initialized else user_data_dir() / "logs"
        return LogStore(directory, self.redactor)

    @cached_property
    def state(self) -> StateStore | None:
        return StateStore(self.db) if (self.db and self.initialized) else None

    @cached_property
    def tracer(self) -> Tracer:
        return Tracer(self.db)

    # ------------------------------------------------------------------ engine
    @cached_property
    def prompter(self) -> ConsolePrompter:
        return ConsolePrompter(self.output.err_console, interactive=self.options.is_interactive())

    @cached_property
    def approvals(self) -> ApprovalManager:
        try:
            policy = ApprovalPolicy.from_config(self.config.approvals)
        except ValueError as exc:
            raise ConfigError(f"Invalid approvals configuration: {exc}") from exc
        return ApprovalManager(policy, self.prompter, assume_yes=self.options.yes, dry_run=self.options.dry_run)

    @cached_property
    def engine(self) -> Engine:
        if self.initialized:
            try:
                resolved = self.environment.resolve()
                self.ctx.env.update(resolved.values)
                self.redactor.add(self.environment.secret_values())
            except HighhXError as exc:
                self.output.warn(f"environment not loaded: {exc.message}")
        if self.initialized and self.options.verbose:
            if self.loaded_config is None and self._config_error is not None:
                self.output.warn(f"configuration invalid: {self._config_error.message}")
        engine = Engine(
            self.ctx,
            approvals=self.approvals,
            policy=self.policy,
            history=self.history,
            logs=self.logs,
            redactor=self.redactor,
            output=self.output,
            tracer=self.tracer,
            project_name=self.config.project_name or self.root.name,
            facts=self._facts,
        )
        from highhx.observability.events import bridge_to_log

        # Retries, step transitions and approval decisions go into the execution log.
        bridge_to_log(self.ctx.events, lambda: op.log if (op := engine.current_operation) else None)
        if self.history is not None:
            try:
                self.history.mark_stale_running()
            except Exception:
                pass
        return engine

    def _facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if self.git_repo.is_repo():
            facts["branch"] = self.git_repo.current_branch()
        if self.initialized:
            try:
                facts["profile"] = self.environment.active_profile()
            except HighhXError:
                pass
        return facts

    # ------------------------------------------------------------------ cloud
    @cached_property
    def cloud(self) -> CloudAccount:
        """The HighhX platform account (sign-in, plan, AI gateway). Never required by Free commands."""
        from highhx.cloud.account import CloudAccount

        return CloudAccount()

    # --------------------------------------------------------------- services
    @cached_property
    def environment(self) -> EnvironmentManager:
        from highhx.environment.manager import EnvironmentManager
        from highhx.environment.variables import EnvironmentSpec

        spec = EnvironmentSpec.load(self.paths.environment_file)
        if self.config.default_env_profile and not self.paths.environment_file.exists():
            spec.default_profile = self.config.default_env_profile
        return EnvironmentManager(self.root, spec, self.state, override=os.environ.get("HIGHHX_ENV"))

    @cached_property
    def git_repo(self) -> GitRepository:
        from highhx.git.repository import GitRepository

        return GitRepository(self.engine_for_git, self.root)

    @cached_property
    def engine_for_git(self) -> Engine:
        """A lightweight engine for read-only git queries used while building facts."""
        return Engine(self.ctx, approvals=self.approvals, policy=PolicyEngine(), output=None)

    @cached_property
    def git(self) -> GitManager:
        from highhx.git.manager import GitManager
        from highhx.git.repository import GitRepository

        return GitManager(self.engine, GitRepository(self.engine, self.root), self.policy)

    @cached_property
    def plugins(self) -> PluginRegistry:
        from highhx.plugins.loader import load_plugins

        sources = []
        if self.initialized:
            sources.append((self.paths.plugins_dir, self.paths.plugins_lock, "project"))
        user_dir = user_data_dir() / "plugins"
        sources.append((user_dir, user_dir / "plugins.lock.json", "user"))
        return load_plugins(sources, self.config.plugins, self.plugin_trust)

    @cached_property
    def plugin_trust(self) -> TrustStore:
        """Per-user record of trusted plugin contents (never stored in the project)."""
        from highhx.plugins.sandbox import TrustStore

        return TrustStore(user_data_dir() / "trusted-plugins.json")

    @cached_property
    def plugin_manager(self) -> PluginManager:
        from highhx.plugins.manager import PluginLocation, PluginManager

        project = (
            PluginLocation(self.paths.plugins_dir, self.paths.plugins_lock, "project") if self.initialized else None
        )
        user_dir = user_data_dir() / "plugins"
        return PluginManager(
            self.engine,
            project,
            PluginLocation(user_dir, user_dir / "plugins.lock.json", "user"),
            self.config.plugins.index,
            self.root,
            self.plugin_trust,
        )

    @cached_property
    def workflow_loader(self) -> WorkflowLoader:
        from highhx.workflows.loader import WorkflowLoader

        return WorkflowLoader.for_project(self.paths.workflows_dir, self.plugins.workflow_dirs)

    @cached_property
    def workflows(self) -> WorkflowEngine:
        from highhx.ui.reporting import WorkflowConsoleReporter
        from highhx.workflows.engine import WorkflowEngine

        return WorkflowEngine(
            self.engine,
            self.workflow_loader,
            root=self.root,
            reporter=WorkflowConsoleReporter(self.output),
            project_name=self.config.project_name,
        )

    @cached_property
    def service_registry(self) -> ServiceRegistry:
        from highhx.services.registry import ServiceRegistry

        return ServiceRegistry(self.config.services, self.paths.services_state_dir)

    @cached_property
    def services(self) -> ServiceManager:
        from highhx.services.manager import ServiceManager

        return ServiceManager(self.engine, self.root, self.service_registry, self.paths.services_logs_dir)

    @cached_property
    def dependencies(self) -> DependencyManager:
        from highhx.dependencies.manager import DependencyManager

        return DependencyManager(self.engine, self.root, self.profile, self.config)

    @cached_property
    def tests(self) -> TestRunner:
        from highhx.testing.runner import TestRunner

        return TestRunner(self.engine, self.root, self.profile, self.config)

    @cached_property
    def builder(self) -> Builder:
        from highhx.building.builder import Builder

        return Builder(
            self.engine, self.root, self.profile, self.config, self.paths.state_dir if self.initialized else None
        )

    @cached_property
    def releases(self) -> ReleaseManager:
        from highhx.git.repository import GitRepository
        from highhx.release.manager import ReleaseManager

        return ReleaseManager(
            self.engine,
            self.root,
            GitRepository(self.engine, self.root),
            self.config.release,
            self.profile,
            self.policy,
        )

    @cached_property
    def deployments(self) -> DeploymentManager:
        from highhx.deployment.manager import DeploymentManager
        from highhx.deployment.state import DeploymentStore
        from highhx.git.repository import GitRepository

        if self.db is None:
            raise ConfigError("Deployment state storage is unavailable.")
        return DeploymentManager(
            self.engine,
            self.root,
            self.config,
            DeploymentStore(self.db),
            GitRepository(self.engine, self.root),
            self.policy,
            self.profile.version,
        )

    @cached_property
    def database(self) -> DatabaseManager:
        from highhx.database.manager import DatabaseManager

        env = self.environment
        profile = env.active_profile()
        return DatabaseManager(
            self.engine,
            self.root,
            self.config.database,
            env.resolve().values,
            profile=profile,
            protected=env.is_protected(profile),
        )

    @cached_property
    def docker(self) -> DockerClient:
        from highhx.integrations.docker import DockerClient

        return DockerClient(self.engine, self.root)

    def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:
                pass


pass_app = click.make_pass_decorator(App)


def exit_code_for(result: Any) -> int:
    """Process exit code for a CommandResult: 0 on success, 124 timeout, 130 cancel."""
    status = str(getattr(result, "status", ""))
    if getattr(result, "ok", False) or getattr(result, "dry_run", False):
        return 0
    if status == "timeout":
        return 124
    if status == "cancelled":
        return 130
    code = getattr(result, "exit_code", None)
    return code if isinstance(code, int) and 0 < code < 256 else 1
