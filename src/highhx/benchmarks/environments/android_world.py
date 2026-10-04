"""Android tasks in the AndroidWorld style, on a real device or emulator.

AndroidWorld (google-research/android_world) defines a task by four hooks and parameters:

    initialize_task(env)   put the device in the task's starting state
    is_successful(env)     score the final state, 0.0 … 1.0, from the device itself
    tear_down(env)         undo what initialize_task did
    complexity, params     difficulty, and the values this instance was generated with

:class:`AndroidTask` mirrors that contract with an :class:`~highhx.drivers.android.adb.AdbClient`
as the environment, so an AndroidWorld task can be wrapped in a few lines and run by HighhX's
agent loop — every action still goes through the executor — and scored by the task itself, not
by what the agent claims.

Tasks are Python classes registered by name (:func:`register`); a benchmark suite refers to them
by that name, so a YAML file can never import code:

    tasks:
      - id: open-settings
        description: Open the Settings app
        environment: {kind: android_device, task: open_app, params: {package: com.android.settings}}
        planner: {kind: scripted, steps: [{action: launch, parameters: {name: com.android.settings}}]}

Without adb or a ready device the task is reported as skipped with the reason, never as a pass
or a fail. **Status: implemented, not validated on a real device in this build.**
"""

from __future__ import annotations

from typing import Any, ClassVar

from highhx.drivers.android.adb import AdbClient, check_package


class AndroidTask:
    """Base class (AndroidWorld's ``TaskEval`` contract, adb as the environment)."""

    name: ClassVar[str] = ""
    complexity: ClassVar[float] = 1.0
    goal_template: ClassVar[str] = ""
    schema: ClassVar[dict[str, Any]] = {}
    """The parameters the task accepts (JSON Schema style, documentation)."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(params or {})

    @property
    def goal(self) -> str:
        return self.goal_template.format(**self.params) if self.goal_template else ""

    def initialize_task(self, env: AdbClient) -> None:
        """Put the device in the starting state (default: go home)."""
        env.keyevent("home")

    def is_successful(self, env: AdbClient) -> float:
        raise NotImplementedError

    def tear_down(self, env: AdbClient) -> None:
        """Undo :meth:`initialize_task` (default: nothing)."""


_REGISTRY: dict[str, type[AndroidTask]] = {}


def register(cls: type[AndroidTask]) -> type[AndroidTask]:
    """Make a task class available to benchmark suites by its ``name``."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name")
    _REGISTRY[cls.name] = cls
    return cls


def task_class(name: str) -> type[AndroidTask]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no Android task named {name!r} (registered: {', '.join(sorted(_REGISTRY)) or 'none'})"
        ) from None


def registered() -> list[str]:
    return sorted(_REGISTRY)


@register
class OpenApp(AndroidTask):
    """The given app is in the foreground."""

    name = "open_app"
    goal_template = "Open the app {package}"
    schema: ClassVar[dict[str, Any]] = {"package": {"type": "string"}}

    def initialize_task(self, env: AdbClient) -> None:
        check_package(str(self.params["package"]))
        env.stop(str(self.params["package"]))
        env.keyevent("home")

    def is_successful(self, env: AdbClient) -> float:
        package, _activity = env.current_app()
        return 1.0 if package == self.params["package"] else 0.0


@register
class ScreenShowsText(AndroidTask):
    """The screen's UI hierarchy contains a text (e.g. after typing a note)."""

    name = "screen_shows_text"
    goal_template = "Make the screen show {text!r}"
    schema: ClassVar[dict[str, Any]] = {"text": {"type": "string"}}

    def is_successful(self, env: AdbClient) -> float:
        return 1.0 if str(self.params["text"]) in env.ui_dump() else 0.0


class DeviceUnavailable(Exception):
    """No adb, or no ready device: the task cannot run here (skipped, with this reason)."""


def device_for(spec: dict[str, Any], runner: Any = None) -> AdbClient:
    """The adb client for ``environment: {kind: android_device, serial?}``, or DeviceUnavailable."""
    serial = spec.get("serial") or None
    adb = AdbClient(serial=serial, runner=runner)
    capability = adb.capability()
    if not capability.available:
        raise DeviceUnavailable(capability.detail)
    try:
        adb.require_device()
    except Exception as exc:  # AdbError, UsageError: not connected, offline, unauthorized
        raise DeviceUnavailable(str(getattr(exc, "message", exc))) from exc
    return adb
