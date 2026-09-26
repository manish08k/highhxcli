"""highhx benchmark"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.execution.command import join_command
from highhx.testing.benchmark import benchmark as run_benchmark
from highhx.utils.time import format_duration


@click.command(
    "benchmark",
    short_help="Time a command over several runs.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--runs", "-n", type=click.IntRange(1, 1000), default=5, show_default=True)
@click.option("--warmup", type=click.IntRange(0, 100), default=1, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@pass_app
def benchmark(app: App, runs: int, warmup: int, command: tuple[str, ...]) -> int:
    """Run COMMAND (default: the test command) repeatedly and report min/mean/median/max."""
    text = (command[0] if len(command) == 1 else join_command(command)) if command else app.commands().get("test")
    if not text:
        raise NotFoundError("Nothing to benchmark.", hint="Pass a command: highhx benchmark -- pytest -q")
    result = run_benchmark(app.engine, app.root, text, runs=runs, warmup=warmup)
    if not result.durations:
        return 0
    data = result.to_dict()
    out = app.output
    out.emit(
        data,
        lambda: out.kv(
            {
                "command": text,
                "runs": f"{runs} (+{warmup} warm-up)",
                "min": format_duration(data["min"]),
                "mean": format_duration(data["mean"]),
                "median": format_duration(data["median"]),
                "max": format_duration(data["max"]),
                "stdev": format_duration(data["stdev"]),
            },
            title="Benchmark",
        ),
    )
    return 0
