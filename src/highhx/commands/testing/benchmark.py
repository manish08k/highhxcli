"""highhx benchmark"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import NotFoundError
from highhx.execution.command import join_command
from highhx.testing.benchmark import benchmark as run_benchmark
from highhx.utils.time import format_duration


@click.group(
    "benchmark",
    cls=DefaultGroup,
    default_command="time",
    short_help="Time a command, or measure the computer-use runtime on benchmark suites.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
def benchmark() -> None:
    """`highhx benchmark [-- COMMAND]` times a command over several runs (the default).
    `list` / `run` / `report` / `compare` measure the computer-use runtime on benchmark suites:
    task success rate, grounding accuracy, selector healing rate, recovery success rate,
    verification accuracy, average retries, completion time and token/tool cost."""


@benchmark.command(
    "time",
    short_help="Time a command over several runs.",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--runs", "-n", type=click.IntRange(1, 1000), default=5, show_default=True)
@click.option("--warmup", type=click.IntRange(0, 100), default=1, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@pass_app
def benchmark_time(app: App, runs: int, warmup: int, command: tuple[str, ...]) -> int:
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


def _format(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return f"{value.get('mean', 0):.2f}s"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


@benchmark.command("list", short_help="Benchmark suites.")
@pass_app
def benchmark_list(app: App) -> int:
    """The built-in suites (deterministic: simulated web app, desktop, Android device and
    temporary workspaces) and how many tasks each has."""
    from highhx.benchmarks.runner import builtin_suites, load_suite

    data = []
    for name in builtin_suites():
        suite = load_suite(name)
        data.append({"suite": name, "tasks": len(suite.tasks), "description": suite.description})
    app.output.emit(data, lambda: app.output.table(["suite", "tasks", "description"], [(d["suite"], d["tasks"], d["description"]) for d in data]))
    return 0


@benchmark.command("run", short_help="Run a benchmark suite.")
@click.argument("suites", nargs=-1, required=True)
@click.option("--runs", "-n", type=click.IntRange(1, 100), default=1, show_default=True, help="Repetitions of each task.")
@click.option("--model", is_flag=True, help="Also run model-planned tasks (HighhX Pro or a local model).")
@click.option("--remote-model", is_flag=True, help="Allow a remote model.")
@pass_app
def benchmark_run(app: App, suites: tuple[str, ...], runs: int, model: bool, remote_model: bool) -> int:
    """Run SUITES (built-in names or YAML files): each task in a fresh environment on the real
    executor and agent loop, scored by an independent evaluator. Results are saved for
    `report` and `compare`."""
    from highhx.benchmarks import BenchmarkRunner, BenchmarkStore, load_suite

    factory = None
    if model:
        from highhx.models.registry import language_model

        def factory(target_app: object) -> object:
            return language_model(app, app.ctx.cancel, remote_ok=remote_model)

    def progress(name: str, data: dict[str, object]) -> None:
        if name == "benchmark.task" and app.output.human:
            if data.get("skipped"):
                app.output.note(f"  {data.get('task')}: skipped ({data['skipped']})")
            else:
                mark = "✓" if data.get("success") else "✗"
                app.output.note(f"  {mark} {data.get('task')} #{data.get('run')} · {data.get('status')} · {float(str(data.get('seconds') or 0)):.2f}s")

    store = BenchmarkStore.for_app(app)
    results = []
    worst = 0
    for name in suites:
        suite = load_suite(name)
        if app.output.human:
            app.output.heading(f"{suite.name} — {len(suite.tasks)} task(s) x {runs}")
        result = BenchmarkRunner(runs=runs, model_factory=factory, on_event=progress).run_suite(suite)
        store.save(result)
        results.append(result.to_dict())
        summary = result.to_dict()["summary"]
        if summary.get("task_success_rate") not in (None, 1.0):
            worst = 1

    def render() -> None:
        for data in results:
            s = data["summary"]
            app.output.kv(
                {
                    "benchmark": data["benchmark_id"],
                    "task success rate": _format(s.get("task_success_rate")),
                    "grounding accuracy": _format(s.get("grounding_accuracy")),
                    "selector healing rate": _format(s.get("selector_healing_rate")),
                    "recovery success rate": _format(s.get("recovery_success_rate")),
                    "verification accuracy": _format(s.get("verification_accuracy")),
                    "average retries": _format(s.get("average_retries")),
                    "completion time": _format(s.get("completion_time")),
                    "token / tool cost": f"{s.get('token_cost', 0)} / {s.get('tool_cost', 0)}",
                },
                title=data["suite"],
            )

    app.output.emit(results if len(results) > 1 else results[0], render)
    return worst


@benchmark.command("report", short_help="A saved benchmark result.")
@click.argument("benchmark_id", required=False)
@click.option("--list", "list_", is_flag=True, help="List saved results.")
@pass_app
def benchmark_report(app: App, benchmark_id: str | None, list_: bool) -> int:
    """Per task and overall (default: the latest result)."""
    from highhx.benchmarks import BenchmarkStore

    store = BenchmarkStore.for_app(app)
    if list_:
        ids = store.ids()
        app.output.emit(ids, lambda: app.output.lines(ids, empty="(no results yet)"))
        return 0
    result = store.load(benchmark_id).to_dict()
    columns = ["task", "runs", "success", "grounding", "healing", "recovery", "verification", "retries", "time"]

    def render() -> None:
        rows = [
            (task, t["runs"], _format(t["task_success_rate"]), _format(t["grounding_accuracy"]), _format(t["selector_healing_rate"]), _format(t["recovery_success_rate"]), _format(t["verification_accuracy"]), _format(t["average_retries"]), _format(t["completion_time"]))
            for task, t in result["tasks"].items()
        ]
        s = result["summary"]
        rows.append(("ALL", s["runs"], _format(s["task_success_rate"]), _format(s["grounding_accuracy"]), _format(s["selector_healing_rate"]), _format(s["recovery_success_rate"]), _format(s["verification_accuracy"]), _format(s["average_retries"]), _format(s["completion_time"])))
        app.output.table(columns, rows, title=f"{result['suite']} · {result['benchmark_id']}")

    app.output.emit(result, render)
    return 0


@benchmark.command("compare", short_help="Compare two benchmark results.")
@click.argument("base")
@click.argument("other", required=False)
@pass_app
def benchmark_compare(app: App, base: str, other: str | None) -> int:
    """Metric changes from BASE to OTHER (default: the latest result)."""
    from highhx.benchmarks import BenchmarkStore, compare

    store = BenchmarkStore.for_app(app)
    diff = compare(store.load(base), store.load(other))

    def render() -> None:
        rows = []
        for metric, d in diff["summary"].items():
            mark = "" if d["better"] is None else ("better" if d["better"] else "worse")
            rows.append((metric, _format(d["base"]), _format(d["other"]), _format(d["delta"]), mark))
        app.output.table(["metric", diff["base"], diff["other"], "change", ""], rows)

    app.output.emit(diff, render)
    return 0
