"""highhx git tag"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("tag", short_help="List tags or create an annotated tag.")
@click.argument("name", required=False)
@click.option("--message", "-m", help="Tag message.")
@click.option("--push", is_flag=True, help="Push the tag to origin (asks for confirmation).")
@pass_app
def tag(app: App, name: str | None, message: str | None, push: bool) -> int:
    """Without NAME, list tags newest first. With NAME, create an annotated tag at HEAD;
    --push also pushes it to origin (dangerous: asks for confirmation)."""
    manager = app.git
    out = app.output
    if name is None:
        manager.repo.require()
        tags = manager.repo.tags()
        out.emit(
            {"tags": [t.to_dict() for t in tags]},
            lambda: (
                out.table(
                    ["tag", "date", "commit", "message"], [(t.name, t.date[:10], t.commit, t.message) for t in tags]
                )
                if tags
                else out.info("No tags.")
            ),
        )
        return 0
    manager.tag(name, message=message, push=push)
    out.emit(
        {"tag": name, "pushed": push}, lambda: out.success(f"Created tag {name}" + (" and pushed it" if push else ""))
    )
    return 0
