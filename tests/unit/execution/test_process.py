import os
import threading
import time
from pathlib import Path

from highhx.execution.cancellation import CancellationToken
from highhx.execution.process import run_process
from tests.conftest import py_cmd


def test_captures_stdout_stderr_and_exit_code() -> None:
    outcome = run_process(py_cmd("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"))
    assert outcome.exit_code == 3
    assert outcome.stdout == "out\n"
    assert outcome.stderr == "err\n"


def test_streams_lines_in_order() -> None:
    seen: list[tuple[str, str]] = []
    run_process(
        py_cmd("import sys\nfor i in range(3): print(i, flush=True)"), on_line=lambda s, line: seen.append((s, line))
    )
    assert seen == [("stdout", "0"), ("stdout", "1"), ("stdout", "2")]


def test_environment_and_working_directory(tmp_path: Path) -> None:
    env = {**os.environ, "HIGHHX_TEST_VALUE": "42"}
    outcome = run_process(
        py_cmd("import os; print(os.environ['HIGHHX_TEST_VALUE'], os.getcwd())"), env=env, cwd=tmp_path
    )
    value, cwd = outcome.stdout.split()
    assert value == "42"
    assert Path(cwd).resolve() == tmp_path.resolve()


def test_timeout_terminates_process() -> None:
    started = time.monotonic()
    outcome = run_process(py_cmd("import time; time.sleep(30)"), timeout=0.5)
    assert outcome.timed_out
    assert time.monotonic() - started < 10


def test_cancellation_terminates_process() -> None:
    token = CancellationToken()
    threading.Timer(0.3, token.cancel).start()
    outcome = run_process(py_cmd("import time; time.sleep(30)"), cancel=token)
    assert outcome.cancelled


def test_missing_command_reports_127() -> None:
    outcome = run_process(["definitely-not-a-real-command-xyz"])
    assert outcome.exit_code == 127
    assert outcome.error and "command not found" in outcome.error


def test_stdin_data_is_fed() -> None:
    outcome = run_process(py_cmd("import sys; print(sys.stdin.read().upper())"), stdin_data="hello")
    assert outcome.stdout.strip() == "HELLO"


def test_output_capture_is_bounded() -> None:
    outcome = run_process(py_cmd("print('x' * 1000)\n" * 50), max_capture=5000)
    assert "truncated" in outcome.stdout
    assert len(outcome.stdout) < 7000
