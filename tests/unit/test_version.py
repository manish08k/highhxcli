"""One version, everywhere: `highhx.__version__` is the single source of truth.

The wheel's metadata is generated from it (hatch reads `src/highhx/__init__.py`), and every
display — `highhx --version`, the banners, the platform user agent — imports it.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from highhx import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_package_metadata_is_generated_from_highhx_version() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in config["project"]["dynamic"] and "version" not in config["project"]
    assert config["tool"]["hatch"]["version"]["path"] == "src/highhx/__init__.py"


def test_the_version_is_written_down_once() -> None:
    literal = re.compile(rf"[\"']{re.escape(__version__)}[\"']")
    places = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src" / "highhx").rglob("*.py")
        if literal.search(path.read_text(encoding="utf-8"))
    ]
    assert places == ["src/highhx/__init__.py"]


def test_version_flag_and_banners_use_it(tmp_path: Path) -> None:
    from highhx.cli import cli

    assert CliRunner().invoke(cli, ["--version"]).output.strip() == f"highhx {__version__}"
    for module in ("src/highhx/cli.py", "src/highhx/agent/ui.py"):
        source = (ROOT / module).read_text()
        assert 'f"HighhX v{__version__}"' in source, module


def test_installed_metadata_matches() -> None:
    """Fails when the environment runs a stale install (e.g. an editable install made before
    the last version bump): reinstall with `pip install -e .`."""
    try:
        installed = importlib.metadata.version("highhxcli")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("highhxcli is not installed in this environment")
    assert installed == __version__, f"installed metadata says {installed}; reinstall with `pip install -e .`"
