"""highhx git status"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("status", short_help="Branch, upstream and changed files.")
@pass_app
def status(app: App) -> int:
    """Show the current branch, its upstream (ahead/behind), and staged, unstaged,
    untracked and conflicted files."""
    st = app.git.status()
    out = app.output

    def render() -> None:
        upstream = f" → {st.upstream} (ahead {st.ahead}, behind {st.behind})" if st.upstream else " (no upstream)"
        out.markup(f"[title]{st.branch or 'detached HEAD'}[/title]{upstream}")
        if st.clean:
            out.success("Working tree clean")
            return
        rows = (
            [(f.index + f.worktree, f.path) for f in st.staged]
            + [(f".{f.worktree}", f.path) for f in st.unstaged if f not in st.staged]
            + [("??", p) for p in st.untracked]
            + [("UU", p) for p in st.conflicted]
        )
        out.table(["state", "path"], rows)
        out.note(
            f"{len(st.staged)} staged · {len(st.unstaged)} unstaged · {len(st.untracked)} untracked · {len(st.conflicted)} conflicted"
        )

    out.emit(st.to_dict(), render)
    return 0
