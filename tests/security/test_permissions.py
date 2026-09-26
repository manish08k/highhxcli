import os
import stat
from pathlib import Path

import pytest

from highhx.security.permissions import check_permissions

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def test_loose_permissions_are_reported(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    env.chmod(0o644)
    key = tmp_path / "id_rsa"
    key.write_text("x")
    key.chmod(0o666)
    rules = {(f.rule, f.path) for f in check_permissions(tmp_path, [key])}
    assert ("sensitive-file-readable", ".env") in rules
    assert ("world-writable", "id_rsa") in rules


def test_repair_fixes_env_permissions(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cli("init", cwd=tmp_path)
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    env.chmod(0o644)
    assert cli("repair", cwd=tmp_path).code == 0
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_hooks_created_executable_by_owner_only_write(tmp_path: Path) -> None:
    from highhx.automation.hooks import install_hook
    from tests.conftest import init_repo, requires_git

    if requires_git.args[0]:
        pytest.skip("git missing")
    (tmp_path / "f").write_text("x")
    init_repo(tmp_path)
    mode = stat.S_IMODE(install_hook(tmp_path, "pre-commit").stat().st_mode)
    assert mode & stat.S_IXUSR and not mode & stat.S_IWOTH
