"""highhx debug"""

from __future__ import annotations

import json
import os
import sys

import click

from highhx import __version__
from highhx.commands import App, pass_app
from highhx.detection.operating_system import detect_os
from highhx.environment.secrets import masked
from highhx.utils.paths import user_config_dir, user_data_dir


@click.command("debug", short_help="Print a redacted debug bundle for bug reports.")
@pass_app
def debug(app: App) -> int:
    """Versions, paths, detection results and HIGHHX_* variables — secrets are
    masked, so the output is safe to paste into an issue."""
    from importlib.metadata import PackageNotFoundError, version

    def dist_version(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:
            return "not installed"

    data = {
        "highhx": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "libraries": {
            "click": dist_version("click"),
            "rich": dist_version("rich"),
            "pyyaml": dist_version("PyYAML"),
        },
        "os": detect_os().to_dict(),
        "paths": {
            "cwd": str(app.start_dir),
            "root": str(app.root),
            "initialized": app.initialized,
            "config": str(app.paths.config_file),
            "user_config": str(user_config_dir()),
            "user_data": str(user_data_dir()),
        },
        "options": dict(app.options.__dict__.items()),
        "environment": masked(
            {k: v for k, v in os.environ.items() if k.startswith("HIGHHX_") or k in ("NO_COLOR", "CI", "TERM")}
        ),
        "project": app.profile.to_dict(),
        "plugins": [p.to_dict() for p in app.plugins.plugins],
    }
    if app.options.json:
        app.output.json(data)
    else:
        click.echo(json.dumps(data, indent=2, default=str))
    return 0
