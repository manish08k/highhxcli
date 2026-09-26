from highhx.core.errors import DependencyCycleError, ExitCode, ToolNotFoundError
from highhx.core.events import EventBus


def test_errors_carry_exit_codes_and_hints() -> None:
    error = ToolNotFoundError("docker", purpose="run containers")
    assert error.exit_code == ExitCode.MISSING_TOOL
    assert "docker" in error.message and error.hint
    cycle = DependencyCycleError(["a", "b", "a"])
    assert "a -> b -> a" in cycle.message
    assert cycle.to_dict()["exit_code"] == ExitCode.VALIDATION


def test_event_bus_isolates_failing_handlers() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("x", lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))
    unsubscribe = bus.subscribe("x", lambda e: received.append(e.data["name"]))
    bus.emit("x", name="first")
    unsubscribe()
    bus.emit("x", name="second")
    assert received == ["first"]
