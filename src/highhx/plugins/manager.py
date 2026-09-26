"""Installing, removing, updating and searching plugins."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.engine import Engine
from highhx.core.errors import NotFoundError, PluginError
from highhx.execution.command import CommandSpec
from highhx.plugins.loader import plugin_directories
from highhx.plugins.manifest import PluginManifest, load_manifest
from highhx.plugins.sandbox import LockEntry, TrustStore, read_lock, symlinked_files, write_lock
from highhx.utils.filesystem import ensure_dir
from highhx.utils.hashing import sha256_tree
from highhx.utils.time import iso_now
from highhx.utils.validation import is_identifier


@dataclass
class PluginLocation:
    directory: Path
    lock: Path
    scope: str


@dataclass
class IndexEntry:
    name: str
    description: str
    source: str
    version: str | None = None
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def is_git_source(source: str) -> bool:
    return source.startswith(("https://", "http://", "git@", "ssh://", "git+")) or source.endswith(".git")


class PluginManager:
    def __init__(
        self,
        engine: Engine,
        project: PluginLocation | None,
        user: PluginLocation,
        index_sources: list[str],
        root: Path,
        trust: TrustStore | None = None,
    ) -> None:
        self.engine = engine
        self.project = project
        self.user = user
        self.index_sources = index_sources
        self.root = root
        self.trust = trust

    def location(self, global_: bool) -> PluginLocation:
        if global_ or self.project is None:
            return self.user
        return self.project

    def installed(self) -> list[tuple[PluginManifest | None, PluginLocation, str | None]]:
        rows: list[tuple[PluginManifest | None, PluginLocation, str | None]] = []
        for loc in (self.project, self.user):
            if loc is None:
                continue
            for directory in plugin_directories(loc.directory):
                try:
                    rows.append((load_manifest(directory), loc, None))
                except PluginError as exc:
                    rows.append((None, loc, f"{directory.name}: {exc.message}"))
        return rows

    # ----------------------------------------------------------- index
    def index(self) -> list[IndexEntry]:
        entries: list[IndexEntry] = []
        for source in self.index_sources:
            path = (self.root / source) if not Path(source).is_absolute() else Path(source)
            if path.is_dir():
                for directory in plugin_directories(path):
                    try:
                        manifest = load_manifest(directory)
                    except PluginError:
                        continue
                    entries.append(
                        IndexEntry(manifest.name, manifest.description, str(directory), manifest.version, source)
                    )
            elif path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise PluginError(f"Plugin index {source} is not valid JSON: {exc}") from exc
                for item in data.get("plugins", data) if isinstance(data, dict) else data:
                    entries.append(
                        IndexEntry(
                            item["name"], item.get("description", ""), item["source"], item.get("version"), source
                        )
                    )
        return entries

    def search(self, query: str) -> list[dict[str, Any]]:
        needle = query.lower()
        results: list[dict[str, Any]] = []
        installed = {m.name: loc.scope for m, loc, _ in self.installed() if m is not None}
        for entry in self.index():
            if needle in entry.name.lower() or needle in entry.description.lower():
                results.append({**entry.to_dict(), "installed": installed.get(entry.name)})
        return results

    # --------------------------------------------------------- install
    def _fetch(self, source: str, workdir: Path) -> Path:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = self.root / source
        if path.is_dir():
            return path
        if is_git_source(source):
            url = source[4:] if source.startswith("git+") else source
            target = workdir / "clone"
            self.engine.approve(
                f"Download plugin from {url} (network access)", RiskLevel.NORMAL, policy_action="plugin:download"
            )
            result = self.engine.run(
                CommandSpec(["git", "clone", "--depth", "1", url, str(target)], name="plugin-clone"), approved=True
            )
            self.engine.raise_for(result, hint="Check the URL and your network connection.")
            shutil.rmtree(target / ".git", ignore_errors=True)
            return target
        for entry in self.index():
            if entry.name == source:
                return self._fetch(entry.source, workdir)
        raise NotFoundError(
            f"Plugin source '{source}' not found.",
            hint="Use a local directory, a git URL, or a name from `highhx plugin search`.",
        )

    def install(self, source: str, *, global_: bool = False, force: bool = False) -> PluginManifest:
        location = self.location(global_)
        with tempfile.TemporaryDirectory(prefix="highhx-plugin-") as tmp:
            if self.engine.dry_run and is_git_source(source):
                raise PluginError(f"Dry run: would download the plugin from {source}; nothing was installed.")
            fetched = self._fetch(source, Path(tmp))
            manifest = load_manifest(fetched)
            links = symlinked_files(fetched)
            if links:
                raise PluginError(
                    f"Plugin '{manifest.name}' contains symbolic links, which are not allowed.",
                    details=links[:10],
                    hint="Replace the links with real files.",
                )
            destination = location.directory / manifest.name
            if destination.exists() and not force:
                raise PluginError(
                    f"Plugin '{manifest.name}' is already installed ({location.scope}).",
                    hint="Use `highhx plugin update` or --force.",
                )
            details = [
                f"{manifest.name} {manifest.version} — {manifest.description or 'no description'}",
                f"permissions: {', '.join(manifest.permissions) or 'none'}",
                f"contains Python code: {'yes (' + str(manifest.entry) + ')' if manifest.has_code else 'no'}",
                f"install to: {destination}",
            ]
            risk = RiskLevel.DANGEROUS if manifest.has_code else RiskLevel.NORMAL
            self.engine.approve(
                f"Install plugin '{manifest.name}'", risk, details=details, policy_action="plugin:install"
            )
            if self.engine.dry_run:
                return manifest
            ensure_dir(location.directory)
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(fetched, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        installed = load_manifest(destination)
        digest = sha256_tree(destination)
        lock = read_lock(location.lock)
        lock[installed.name] = LockEntry(
            installed.name, installed.version, source, digest, iso_now(), installed.has_code, installed.permissions
        )
        write_lock(location.lock, lock)
        if self.trust is not None:
            # The user just approved installing these exact files.
            self.trust.trust(digest, installed, source)
        return installed

    def trust_installed(self, name: str, *, global_: bool = False) -> PluginManifest:
        """Trust the current contents of an installed plugin (e.g. one committed to the repository)."""
        location = self._find(name, global_)
        manifest = load_manifest(location.directory / name)
        links = symlinked_files(manifest.directory)
        if links:
            raise PluginError(f"Plugin '{name}' contains symbolic links and cannot be trusted.", details=links[:10])
        digest = sha256_tree(manifest.directory)
        self.engine.approve(
            f"Trust plugin '{name}' {manifest.version} ({location.scope}) to run code",
            RiskLevel.DANGEROUS,
            details=[
                f"entry: {manifest.entry or '(no code)'}",
                f"permissions: {', '.join(manifest.permissions) or 'none'}",
                f"sha256: {digest}",
                "Only trust plugins whose code you have reviewed.",
            ],
            policy_action="plugin:trust",
        )
        if not self.engine.dry_run and self.trust is not None:
            self.trust.trust(digest, manifest, str(manifest.directory))
        return manifest

    def remove(self, name: str, *, global_: bool = False) -> Path:
        location = self._find(name, global_)
        directory = location.directory / name
        self.engine.approve(
            f"Remove plugin '{name}' ({location.scope})",
            RiskLevel.NORMAL,
            details=[str(directory)],
            policy_action="plugin:remove",
        )
        if not self.engine.dry_run:
            shutil.rmtree(directory)
            lock = read_lock(location.lock)
            lock.pop(name, None)
            write_lock(location.lock, lock)
        return directory

    def update(self, name: str, *, global_: bool = False) -> PluginManifest:
        location = self._find(name, global_)
        entry = read_lock(location.lock).get(name)
        if entry is None:
            raise PluginError(
                f"Plugin '{name}' has no recorded source.",
                hint="Reinstall it with `highhx plugin install <source> --force`.",
            )
        return self.install(entry.source, global_=location.scope == "user", force=True)

    def _find(self, name: str, global_: bool) -> PluginLocation:
        if not is_identifier(name):
            raise PluginError(f"Invalid plugin name {name!r}.", hint="Plugin names use letters, digits, '-' and '_'.")
        candidates = [self.user] if global_ else [loc for loc in (self.project, self.user) if loc is not None]
        for loc in candidates:
            if (loc.directory / name).is_dir():
                return loc
        raise NotFoundError(f"Plugin '{name}' is not installed.", hint="List plugins with `highhx plugin list`.")
