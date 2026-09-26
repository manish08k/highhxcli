"""highhx watch"""

from __future__ import annotations

import threading

import click

from highhx.automation.watcher import ChangeSet, FileWatcher
from highhx.commands import App, pass_app
from highhx.config.schema import WatchConfig
from highhx.core.errors import HighhXError, UsageError
from highhx.execution.command import CommandSpec, join_command


def _runner(app: App, watch: WatchConfig):  # type: ignore[no-untyped-def]
    def on_change(changes: ChangeSet) -> None:
        shown = ", ".join(changes.all[:5]) + (" …" if len(changes.all) > 5 else "")
        app.output.info(f"[{watch.name}] {len(changes.all)} change(s): {shown}")
        try:
            if watch.workflow:
                result = app.workflows.run(watch.workflow)
                ok = result.ok
            else:
                assert watch.run
                ok = app.engine.run(
                    CommandSpec(watch.run, cwd=app.root, name=f"watch:{watch.name}"),
                    action=f"Run on change: {watch.run}",
                ).ok
        except HighhXError as exc:
            app.output.error(exc.message)
            ok = False
        (app.output.success if ok else app.output.warn)(
            f"[{watch.name}] {'done' if ok else 'failed'} — watching for changes"
        )

    return on_change


@click.command(
    "watch",
    short_help="Re-run a command or workflow when files change.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--workflow", "-w", metavar="NAME", help="Workflow to run on change.")
@click.option("--path", "-p", "paths", multiple=True, metavar="PATH", help="Paths to watch (default: project root).")
@click.option("--pattern", "patterns", multiple=True, metavar="GLOB", help="Only react to matching files, e.g. '*.py'.")
@click.option("--ignore", "ignores", multiple=True, metavar="GLOB", help="Ignore matching files.")
@click.option("--debounce", default=0.5, show_default=True, type=float, help="Seconds to wait for changes to settle.")
@click.option("--interval", default=0.5, show_default=True, type=float, help="Polling interval in seconds.")
@click.option("--initial/--no-initial", default=True, help="Run once at startup.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@pass_app
def watch(
    app: App,
    workflow: str | None,
    paths: tuple[str, ...],
    patterns: tuple[str, ...],
    ignores: tuple[str, ...],
    debounce: float,
    interval: float,
    initial: bool,
    command: tuple[str, ...],
) -> int:
    """Watch files and run COMMAND (or --workflow) on every change.

    Without COMMAND or --workflow, all watchers from `watch:` in
    .highhx/config.yaml run together. Stop with Ctrl+C. Uses portable polling,
    so it works the same on macOS, Linux and Windows.
    """
    if command and workflow:
        raise UsageError("Give either a COMMAND or --workflow, not both.")
    if command or workflow:
        watchers = [
            WatchConfig(
                "watch",
                list(paths) or ["."],
                list(patterns),
                list(ignores),
                (command[0] if len(command) == 1 else join_command(command)) if command else None,
                workflow,
                debounce,
            )
        ]
    else:
        watchers = app.load_config().watch
        if not watchers:
            raise UsageError(
                "Nothing to watch.",
                hint="Pass a command (`highhx watch pytest`), --workflow, or configure `watch:` in .highhx/config.yaml.",
            )
    if app.options.dry_run:
        for w in watchers:
            app.output.info(
                f"[dry-run] would watch {', '.join(w.paths)} and run {w.run or 'workflow ' + str(w.workflow)}"
            )
        return 0
    cancel = app.ctx.cancel
    threads = []
    for config in watchers:
        callback = _runner(app, config)
        if initial:
            callback(ChangeSet(modified=["(initial run)"]))
        watcher = FileWatcher(
            app.root, paths=config.paths, patterns=config.patterns, ignore=config.ignore, interval=interval
        )
        thread = threading.Thread(
            target=watcher.watch,
            args=(callback,),
            kwargs={"cancel": cancel, "debounce": config.debounce},
            daemon=True,
            name=f"watch-{config.name}",
        )
        thread.start()
        threads.append(thread)
        app.output.info(f"[{config.name}] watching {', '.join(config.paths)} — Ctrl+C to stop")
    try:
        while not cancel.wait(0.5):
            pass
    except KeyboardInterrupt:
        cancel.cancel("interrupted")
    for thread in threads:
        thread.join(timeout=2)
    app.output.info("Stopped watching.")
    return 0
