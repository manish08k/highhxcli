"""Computer drivers: one :class:`ComputerDriver` interface over the desktop (macOS, Windows,
Linux, a remote computer over SSH), the HighhX browser and Android. Drivers are mechanism:
catalog action handlers use them after the executor approved the action. See
docs/COMPUTER_RUNTIME.md."""

from highhx.drivers.base import OPERATIONS, CapabilityError, ComputerDriver, DriverCapabilities

__all__ = ["OPERATIONS", "CapabilityError", "ComputerDriver", "DriverCapabilities", "discover"]


def discover() -> list[dict[str, object]]:
    """Which drivers can run here, without starting anything (for ``highhx computer status``)."""
    import sys

    from highhx.computer.browser import find_browser
    from highhx.drivers.android.adb import INSTALL_HINT, find_adb
    from highhx.drivers.vm import DETAIL as VM_DETAIL

    adb = find_adb()
    browser = find_browser()
    return [
        {"driver": "desktop", "available": True, "detail": f"built-in engine for {sys.platform} (see `highhx computer status`)"},
        {"driver": "browser", "available": bool(browser), "detail": browser or "no Chromium-family browser found"},
        {"driver": "android", "available": bool(adb), "detail": adb or f"adb not found. {INSTALL_HINT}"},
        {"driver": "remote", "available": True, "detail": "set HIGHHX_COMPUTER_TARGET=ssh://user@host (needs HighhX on that computer)"},
        {"driver": "vm", "available": False, "detail": VM_DETAIL},
    ]
