"""highhx artifacts"""

from __future__ import annotations

import click

from highhx.building.artifacts import find_artifacts, verify
from highhx.commands import App, pass_app
from highhx.utils.filesystem import human_size


@click.command("artifacts", short_help="List build artifacts and verify checksums.")
@click.option("--verify", "do_verify", is_flag=True, help="Compare files with the checksums recorded at build time.")
@pass_app
def artifacts(app: App, do_verify: bool) -> int:
    """List artifacts in dist/, build/, target/ … with size and SHA-256."""
    items = find_artifacts(app.root)
    problems = verify(app.root, app.paths.state_dir) if do_verify else []
    out = app.output

    def render() -> None:
        if items:
            out.table(
                ["artifact", "size", "modified", "sha256"],
                [(a.path, human_size(a.size), a.modified, a.sha256[:16]) for a in items],
            )
        else:
            out.info("No artifacts found. Run `highhx build` or `highhx package`.")
        if do_verify:
            if problems:
                for p in problems:
                    out.error(f"{p['path']}: {p['problem']}")
            else:
                out.success("All recorded artifacts match their checksums.")

    out.emit({"artifacts": [a.to_dict() for a in items], "verification": problems if do_verify else None}, render)
    return 1 if problems else 0
