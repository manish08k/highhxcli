import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from highhx.automation.cron import CronExpression
from highhx.automation.hooks import MARKER, install_hook, is_managed, uninstall_hook
from highhx.automation.scheduler import ScheduleRunner
from highhx.automation.watcher import FileWatcher, diff_snapshots
from highhx.config.schema import ScheduleConfig
from highhx.core.errors import ValidationError
from highhx.execution.cancellation import CancellationToken
from tests.conftest import init_repo, requires_git


def test_cron_parsing_and_next_run() -> None:
    expr = CronExpression.parse("*/15 9-17 * * mon-fri")
    assert expr.next_after(datetime(2026, 9, 26, 16, 50)) == datetime(2026, 9, 28, 9, 0)  # Saturday -> Monday
    assert CronExpression.parse("@daily").next_after(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 2, 0, 0)
    assert CronExpression.parse("0 0 1 1 *").next_after(datetime(2026, 6, 1)) == datetime(2027, 1, 1)
    assert CronExpression.parse("0 12 * * 7").matches(datetime(2026, 9, 27, 12, 0))  # Sunday as 7
    for bad in ("* * *", "60 * * * *", "*/0 * * * *", "a * * * *"):
        with pytest.raises(ValueError):
            CronExpression.parse(bad)


def test_schedule_runner_due_and_state() -> None:
    now = [datetime(2026, 1, 1, 10, 0)]
    ran: list[str] = []
    runner = ScheduleRunner(
        [ScheduleConfig("hourly", "0 * * * *", run="x")],
        lambda s: ran.append(s.name) or True,
        None,
        clock=lambda: now[0],
    )
    assert runner.tick() == [("hourly", True)]
    now[0] = datetime(2026, 1, 1, 10, 30)
    assert runner.tick() == []
    now[0] = datetime(2026, 1, 1, 11, 0)
    assert runner.tick() == [("hourly", True)]
    assert ran == ["hourly", "hourly"]


def test_watcher_detects_changes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "notes.txt").write_text("x")
    watcher = FileWatcher(tmp_path, patterns=["*.py"], interval=0.05)
    token = CancellationToken()
    batches = []

    def change() -> None:
        time.sleep(0.2)
        (tmp_path / "a.py").write_text("22")
        (tmp_path / "b.py").write_text("new")
        (tmp_path / "notes.txt").write_text("ignored")

    threading.Thread(target=change).start()
    delivered = watcher.watch(batches.append, cancel=token, debounce=0.1, max_events=1)
    assert delivered == 1
    assert batches[0].modified == ["a.py"] and batches[0].added == ["b.py"]
    assert diff_snapshots({"x": (1, 1)}, {}).removed == ["x"]


@requires_git
def test_git_hook_install_and_uninstall(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    init_repo(tmp_path)
    path = install_hook(tmp_path, "pre-commit")
    assert is_managed(path) and MARKER in path.read_text()
    existing = tmp_path / ".git" / "hooks" / "pre-push"
    existing.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(ValidationError):
        install_hook(tmp_path, "pre-push")
    install_hook(tmp_path, "pre-push", force=True)
    assert uninstall_hook(tmp_path, "pre-push")
    assert existing.read_text() == "#!/bin/sh\necho mine\n"
    with pytest.raises(ValidationError):
        install_hook(tmp_path, "not-a-hook")
