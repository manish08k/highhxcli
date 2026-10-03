"""Android automation through adb: device discovery, input, screenshots, the uiautomator
hierarchy and apps. Every action is a catalog action (``android.*``) run by the executor. See
docs/ANDROID.md."""

from highhx.drivers.android.adb import AdbClient, AdbError, Device, find_adb, parse_devices
from highhx.drivers.android.driver import AndroidDriver
from highhx.drivers.android.hierarchy import parse_hierarchy

__all__ = ["AdbClient", "AdbError", "AndroidDriver", "Device", "find_adb", "parse_devices", "parse_hierarchy"]
