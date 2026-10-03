"""Android as a perception source (for ``computer.state`` and the AndroidDriver)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from highhx.computer.providers import Capability
from highhx.drivers.android.adb import AdbClient, AdbError
from highhx.drivers.android.hierarchy import parse_hierarchy
from highhx.perception.providers import Perceived
from highhx.perception.state import DeviceInfo, ScreenshotRef, WindowInfo

if TYPE_CHECKING:
    from highhx.actions.spec import ActionContext, Inputs
    from highhx.execution.cancellation import CancellationToken
    from highhx.perception.engine import PerceptionEngine
    from highhx.perception.providers import VisionProvider


class AndroidHierarchy:
    name = "android-uiautomator"
    source = "android"

    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb

    def capability(self) -> Capability:
        return self.adb.capability()

    def perceive(self, into: Perceived, *, cancel: CancellationToken | None = None) -> None:
        device = self.adb.require_device()
        elements, text = parse_hierarchy(self.adb.ui_dump())
        into.structured += elements
        into.text = text
        package, activity = self.adb.current_app()
        into.active_app = package
        into.active_window = WindowInfo(activity, package, activity, None, True) if package else None
        size = None
        try:
            size = self.adb.screen_size()
        except AdbError:
            size = None
        into.device = DeviceInfo("android", device.model, device.serial, size, 1.0)
        into.viewport = size
        into.metadata["captured_at"] = str(time.time())


class AndroidScreenshots:
    name = "android-screencap"

    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb

    def capability(self) -> Capability:
        return self.adb.capability()

    def capture(self, *, cancel: CancellationToken | None = None) -> ScreenshotRef:
        return ScreenshotRef.from_bytes(self.adb.screencap(), scale=1.0)


def adb_for(ctx: ActionContext, inputs: Inputs) -> AdbClient:
    factory = getattr(ctx.computer(), "android_client", None)
    serial = inputs.get("device") or None
    return factory(serial, ctx.cancel) if factory is not None else AdbClient(serial=serial, cancel=ctx.cancel)


def android_engine(
    ctx: ActionContext, inputs: Inputs, *, vision: VisionProvider | None = None, emit: Callable[..., Any] | None = None
) -> PerceptionEngine:
    from highhx.perception.engine import PerceptionEngine
    from highhx.perception.providers import TesseractOCRProvider

    adb = adb_for(ctx, inputs)
    return PerceptionEngine(
        "android",
        structure=[AndroidHierarchy(adb)],
        screenshot=AndroidScreenshots(adb),
        ocr=TesseractOCRProvider(),
        vision=vision,
        emit=emit,
    )
