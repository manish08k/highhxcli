from pathlib import Path

import pytest
import yaml

from highhx.config.schema import PluginsConfig
from highhx.core.errors import PluginError
from highhx.plugins.loader import load_plugins
from highhx.plugins.manager import PluginLocation, PluginManager
from highhx.plugins.manifest import load_manifest
from highhx.plugins.sandbox import TrustStore

CODE = """
import click
from highhx.plugins.interface import HighhXPlugin


class Plugin(HighhXPlugin):
    def register(self, api):
        @click.command("from-code")
        def cmd():
            click.echo("code plugin ran")

        api.add_command(cmd)
"""


def make_plugin(base: Path, name: str = "demo", *, code: bool = False, permissions: list[str] | None = None) -> Path:
    directory = base / name
    (directory / "workflows").mkdir(parents=True)
    (directory / "workflows" / "extra.yaml").write_text("name: extra\nsteps:\n  - id: a\n    run: echo\n")
    manifest = {
        "name": name,
        "version": "1.0.0",
        "api_version": 1,
        "permissions": permissions if permissions is not None else ["commands", "detectors"],
        "contributes": {
            "commands": [{"name": "hello", "run": "echo hello"}],
            "workflows": ["workflows"],
            "detectors": [{"name": "deno", "files": ["deno.json"]}],
        },
    }
    if code:
        manifest["entry"] = "plugin.py:Plugin"
        (directory / "plugin.py").write_text(CODE)
    (directory / "highhx-plugin.yaml").write_text(yaml.safe_dump(manifest))
    return directory


def test_manifest_validation(tmp_path: Path) -> None:
    good = load_manifest(make_plugin(tmp_path))
    assert good.commands[0].name == "hello" and not good.has_code
    bad = make_plugin(tmp_path, "bad", permissions=[])
    with pytest.raises(PluginError) as info:
        load_manifest(bad)
    assert any("requires the 'commands' permission" in d for d in info.value.details)
    newer = tmp_path / "newer"
    newer.mkdir()
    (newer / "highhx-plugin.yaml").write_text("name: newer\nversion: '1'\napi_version: 99\n")
    with pytest.raises(PluginError):
        load_manifest(newer)


def install(tmp_path: Path, make_engine, source: Path):  # type: ignore[no-untyped-def]
    kit = make_engine(cwd=tmp_path, yes=True, interactive=False)
    project = PluginLocation(tmp_path / ".highhx" / "plugins", tmp_path / ".highhx" / "plugins.lock.json", "project")
    trust = TrustStore(tmp_path / "user" / "trusted.json")
    manager = PluginManager(
        kit.engine,
        project,
        PluginLocation(tmp_path / "user", tmp_path / "user" / "lock.json", "user"),
        [str(source.parent)],
        tmp_path,
        trust,
    )
    return manager, project, trust


def test_install_lock_and_declarative_contributions(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    source = make_plugin(tmp_path / "src")
    manager, project, _ = install(tmp_path, make_engine, source)
    manager.install(str(source))
    assert project.lock.exists() and "demo" in project.lock.read_text()
    registry = load_plugins([(project.directory, project.lock, "project")], PluginsConfig())
    assert "hello" in registry.declarative_commands
    assert registry.workflow_dirs and registry.detectors
    assert manager.search("dem")[0]["installed"] == "project"
    with pytest.raises(PluginError):
        manager.install(str(source))
    manager.remove("demo")
    assert not (project.directory / "demo").exists()


def test_code_plugin_trust_model(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    source = make_plugin(tmp_path / "src", "coded", code=True)
    manager, project, trust = install(tmp_path, make_engine, source)
    manager.install(str(source))
    sources = [(project.directory, project.lock, "project")]
    disabled = load_plugins(sources, PluginsConfig(allow_code=False), trust)
    assert "from-code" not in disabled.click_commands and disabled.plugins[0].problem
    enabled = load_plugins(sources, PluginsConfig(allow_code=True), trust)
    assert "from-code" in enabled.click_commands
    (project.directory / "coded" / "plugin.py").write_text(CODE + "\n# tampered\n")
    tampered = load_plugins(sources, PluginsConfig(allow_code=True), trust)
    assert tampered.plugins[0].status == "blocked" and "from-code" not in tampered.click_commands
    manager.trust_installed("coded")
    assert "from-code" in load_plugins(sources, PluginsConfig(allow_code=True), trust).click_commands


def test_repository_cannot_vouch_for_its_own_code(tmp_path: Path) -> None:
    """A cloned repo ships plugin code, a matching lock file and allow_code: true."""
    from highhx.plugins.sandbox import LockEntry, write_lock
    from highhx.utils.hashing import sha256_tree

    base = tmp_path / "repo" / ".highhx" / "plugins"
    directory = make_plugin(base, "evil", code=True)
    lock = tmp_path / "repo" / ".highhx" / "plugins.lock.json"
    write_lock(lock, {"evil": LockEntry("evil", "1.0.0", "x", sha256_tree(directory), "now", True, ["commands"])})
    registry = load_plugins(
        [(base, lock, "project")], PluginsConfig(allow_code=True), TrustStore(tmp_path / "user.json")
    )
    assert registry.plugins[0].status == "blocked"
    assert "from-code" not in registry.click_commands


def test_symlinks_are_rejected(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    source = make_plugin(tmp_path / "src", "linky")
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (source / "leak.txt").symlink_to(secret)
    manager, project, _ = install(tmp_path, make_engine, source)
    with pytest.raises(PluginError) as info:
        manager.install(str(source))
    assert "symbolic links" in info.value.message
    assert not (project.directory / "linky").exists()


def test_plugin_names_cannot_traverse_paths(tmp_path: Path, make_engine) -> None:  # type: ignore[no-untyped-def]
    manager, _project, _ = install(tmp_path, make_engine, make_plugin(tmp_path / "src"))
    victim = tmp_path / "victim"
    victim.mkdir()
    for name in ("../../victim", "..", "a/b"):
        with pytest.raises(PluginError):
            manager.remove(name)
    assert victim.exists()


def test_unlocked_code_is_never_loaded(tmp_path: Path) -> None:
    base = tmp_path / "plugins"
    make_plugin(base, "dropped", code=True)
    registry = load_plugins(
        [(base, base / "lock.json", "project")], PluginsConfig(allow_code=True), TrustStore(tmp_path / "t.json")
    )
    assert registry.plugins[0].status == "blocked"


def test_disabled_plugins(tmp_path: Path) -> None:
    base = tmp_path / "plugins"
    make_plugin(base)
    registry = load_plugins([(base, base / "lock.json", "project")], PluginsConfig(disabled=["demo"]))
    assert registry.plugins[0].status == "disabled" and not registry.declarative_commands


def test_isolated_commands_do_not_receive_project_secrets(make_engine) -> None:  # type: ignore[no-untyped-def]
    import sys

    from highhx.execution.command import CommandSpec
    from highhx.execution.isolation import isolated_environment

    kit = make_engine()
    kit.engine.ctx.env["DATABASE_PASSWORD"] = "hunter2hunter2"
    code = "import os; print(os.environ.get('DATABASE_PASSWORD', 'absent'))"
    isolated = kit.engine.run(CommandSpec([sys.executable, "-c", code], env_base=isolated_environment()))
    normal = kit.engine.run(CommandSpec([sys.executable, "-c", code]))
    assert isolated.stdout.strip() == "absent"
    assert normal.stdout.strip() == "[REDACTED]"
