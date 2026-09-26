import threading
import time

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
