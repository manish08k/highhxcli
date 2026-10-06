import threading
import time
from pathlib import Path

import pytest

from highhx.execution.cancellation import CancellationToken
from highhx.execution.parallel import run_parallel
from highhx.execution.retry import RetryPolicy
from highhx.execution.timeout import Deadline


def test_exponential_backoff_is_capped() -> None:
    policy = RetryPolicy(attempts=5, delay=1, backoff=2, max_delay=5)
    assert list(policy.delays()) == [1, 2, 4, 5]


def test_retry_from_config_values() -> None:
    assert RetryPolicy.from_value(3).attempts == 3
    policy = RetryPolicy.from_value({"attempts": 2, "delay": "10s", "backoff": 1.5})
    assert (policy.attempts, policy.delay, policy.backoff) == (2, 10.0, 1.5)
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)


def test_deadline() -> None:
    assert Deadline.after(None).remaining() is None
    deadline = Deadline.after(0.05)
    time.sleep(0.06)
    assert deadline.expired


def test_child_tokens_follow_parent() -> None:
    parent = CancellationToken()
    child = parent.child()
    called: list[str] = []
    child.on_cancel(called.append)
    parent.cancel("stop")
    assert child.cancelled and called == ["stop"]
    late: list[str] = []
    child.on_cancel(late.append)
    assert late == ["stop"]


def test_parallel_respects_limit_and_order() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def task(value: int):  # type: ignore[no-untyped-def]
        def _run() -> int:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return value

        return _run

    outcomes = run_parallel([task(i) for i in range(6)], max_workers=2)
    assert [o.value for o in outcomes] == list(range(6))
    assert peak == 2


def test_parallel_stop_when_skips_remaining() -> None:
    outcomes = run_parallel([lambda: 1, lambda: 2, lambda: 3], max_workers=1, stop_when=lambda v: v == 1)
    assert outcomes[0].value == 1
    assert all(o.skipped for o in outcomes[1:])


def test_a_detached_child_no_longer_follows_and_is_not_kept() -> None:
    parent = CancellationToken()
    done, live = parent.child(), parent.child()
    done.detach()
    done.detach()  # twice is harmless
    assert parent._callbacks == [live.cancel]  # the finished child is no longer held
    parent.cancel("stop")
    assert live.cancelled and not done.cancelled
    assert parent._callbacks == []  # a cancelled token keeps no callbacks either


def test_long_sessions_keep_no_token_per_action_workflow_or_task(tmp_path: Path) -> None:
    # Regression: the executor made a child of the session's token per action and never let it
    # go — 2000 actions held 2000 tokens (and what they referenced) for the life of the process.
    from highhx.actions.executor import ActionExecutor
    from highhx.commands import App
    from highhx.core.context import Options
    from highhx.safety.actions import Actor
    from highhx.safety.gate import ActionGate, ApprovalMode
    from highhx.ui.prompts import StaticPrompter

    (tmp_path / "a.txt").write_text("x")
    app = App(Options(interactive=False, yes=True), cwd=tmp_path)
    try:
        gate = ActionGate(app.engine, StaticPrompter(), source="test", mode=ApprovalMode.AUTO_EDIT)
        executor = ActionExecutor(app, gate, actor=Actor.USER)
        root = app.ctx.cancel
        for _ in range(50):
            assert executor.run("filesystem.read", {"path": "a.txt"}).ok
        assert root._callbacks == []
        executor.close()
    finally:
        app.close()
