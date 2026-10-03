"""The event envelope (trace context) and the recorder the TUI and traces consume."""

from __future__ import annotations

import threading

import pytest

from highhx.actions.events import EventLog, read_events
from highhx.core.events import EventBus, current_context, trace_context
from highhx.observability.stream import EventRecorder, records_from_log
from highhx.security.secrets import Redactor


def test_trace_context_stamps_events_and_nests() -> None:
    bus = EventBus()
    with trace_context(trace_id="t1", source="agent"):
        outer = bus.emit("a")
        with trace_context(action_id="x1"):
            inner = bus.emit("b")
        after = bus.emit("c")
    outside = bus.emit("d")
    assert outer.context == {"trace_id": "t1", "source": "agent"}
    assert inner.context == {"trace_id": "t1", "source": "agent", "action_id": "x1"}
    assert after.context == outer.context and outside.context == {} and current_context() == {}


def test_trace_context_refuses_unknown_keys() -> None:
    with pytest.raises(ValueError), trace_context(user="me"):
        pass


def test_context_does_not_leak_across_threads() -> None:
    bus = EventBus()
    seen: list[dict[str, str]] = []
    with trace_context(trace_id="main"):
        thread = threading.Thread(target=lambda: seen.append(bus.emit("t").context))
        thread.start()
        thread.join()
    assert seen == [{}]


def test_recorder_keeps_a_bounded_redacted_stream_with_listeners() -> None:
    bus = EventBus()
    redactor = Redactor()
    redactor.add(["s3cr3t-value"])
    recorder = EventRecorder.attach(bus, capacity=3, redactor=redactor)
    heard: list[str] = []
    recorder.listen(lambda r: heard.append(r.name))
    recorder.listen(lambda r: 1 / 0)  # a broken consumer never breaks the stream
    with trace_context(trace_id="tr"):
        for i in range(5):
            bus.emit(f"e{i}", token="s3cr3t-value", nested={"deep": [1, 2]})
    records = recorder.snapshot()
    assert [r.name for r in records] == ["e2", "e3", "e4"] and heard == [f"e{i}" for i in range(5)]
    assert all("s3cr3t-value" not in str(r.payload) for r in records)
    assert records[-1].trace_id == "tr" and records[-1].payload["nested"] == {"deep": [1, 2]}
    assert [r.name for r in recorder.since(records[0].seq)] == ["e3", "e4"]
    assert len(recorder.for_trace("tr")) == 3
    recorder.close()
    bus.emit("after")
    assert len(recorder) == 3


def test_the_event_log_persists_the_envelope(tmp_path) -> None:
    bus = EventBus()
    log = EventLog(Redactor(), directory=tmp_path)
    log.attach(bus)
    with trace_context(trace_id="tr9", task_id="task_1"):
        bus.emit("plan.created", steps=3)
    (line,) = read_events(directory=tmp_path)
    assert line["trace_id"] == "tr9" and line["task_id"] == "task_1" and line["steps"] == 3
    (record,) = records_from_log([line])
    assert record.trace_id == "tr9" and record.payload == {"steps": 3}
