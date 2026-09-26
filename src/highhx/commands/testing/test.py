"""highhx test"""

from __future__ import annotations

import click

from highhx.automation.watcher import ChangeSet, FileWatcher
from highhx.commands import App, exit_code_for, pass_app
from highhx.core.errors import HighhXError
from highhx.testing.runner import TestRun


def _render(app: App, run: TestRun) -> None:
    out = app.output
    for note in run.notes:
        out.note(note)
    if not run.command:
        return
    s = run.summary
    if s.parsed:
        parts = [f"{s.passed} passed"]
        if s.failed:
            parts.append(f"{s.failed} failed")
        if s.errors:
            parts.append(f"{s.errors} errors")
        if s.skipped:
            parts.append(f"{s.skipped} skipped")
        text = ", ".join(parts)
    else:
        text = f"exit code {run.result.exit_code}"
    if run.result.dry_run:
        return
    (out.success if run.ok else out.error)(
        f"Tests {'passed' if run.ok else 'failed'}: {text} ({run.result.duration:.1f}s, {run.framework.name})"
    )


@click.command(
    "test", short_help="Run tests (auto-detected framework).", context_settings={"ignore_unknown_options": True}
)
@click.option("--watch", "watch_mode", is_flag=True, help="Re-run tests when files change.")
@click.option("--coverage", is_flag=True, help="Collect coverage.")
@click.option("--changed", is_flag=True, help="Only tests related to files changed since HEAD (git).")
@click.option("--base", metavar="REF", help="With --changed: also include changes since REF (e.g. main).")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_app
def test(app: App, watch_mode: bool, coverage: bool, changed: bool, base: str | None, args: tuple[str, ...]) -> int:
    """Discover the test framework (pytest, unittest, Jest, Vitest, npm test,
    flutter test, Maven, Gradle, ctest, go test, cargo test) and run it.

    Extra ARGS are passed to the test runner (put them after `--`).
    Exit code mirrors the test runner's.
    """

    def changed_files() -> list[str] | None:
        if not changed:
            return None
        repo = app.git_repo
        repo.require()
        return repo.changed_files(base)

    def once() -> TestRun:
        run = app.tests.run(coverage=coverage, changed=changed_files(), args=list(args))
        if not app.options.json:
            _render(app, run)
        return run

    if not watch_mode:
        run = once()
        if app.options.json:
            app.output.json(run.to_dict())
        if run.result.dry_run or not run.command:
            return 0
        return exit_code_for(run.result)

    if app.options.json:
        raise click.UsageError("--watch cannot be combined with --json")
    cancel = app.ctx.cancel
    try:
        once()
    except HighhXError as exc:
        app.output.error(exc.message)
    app.output.info("Watching for changes — Ctrl+C to stop")

    def rerun(changes: ChangeSet) -> None:
        app.output.info(
            f"{len(changes.all)} file(s) changed: {', '.join(changes.all[:3])}{' …' if len(changes.all) > 3 else ''}"
        )
        try:
            once()
        except HighhXError as exc:
            app.output.error(exc.message)

    watcher = FileWatcher(app.root, ignore=["*.pyc", ".coverage", "coverage/*", "htmlcov/*"])
    try:
        watcher.watch(rerun, cancel=cancel)
    except KeyboardInterrupt:
        cancel.cancel("interrupted")
    return 0
