"""Computer-use benchmarks: a HighhX task format, deterministic environments (simulated web app,
desktop and Android device, temporary workspaces) plus the optional real browser, a runner that
uses the real executor and agent loop, and the reported metrics. See docs/BENCHMARKS.md."""

from highhx.benchmarks.model import METRICS, BenchmarkResult, BenchmarkSuite, BenchmarkTask, RunMetrics, aggregate
from highhx.benchmarks.runner import BenchmarkApprover, BenchmarkRunner, builtin_suites, load_suite
from highhx.benchmarks.store import BenchmarkStore, compare

__all__ = [
    "METRICS",
    "BenchmarkApprover",
    "BenchmarkResult",
    "BenchmarkRunner",
    "BenchmarkStore",
    "BenchmarkSuite",
    "BenchmarkTask",
    "RunMetrics",
    "aggregate",
    "builtin_suites",
    "compare",
    "load_suite",
]
