"""A small adb client: devices, input, screenshots, the UI hierarchy, apps.

Every call is an argv list run without a host shell. Text that reaches the *device* shell
(``input text``, package names) is validated or quoted, so a typed string can never become a
device command. Binary output (``screencap``) is read with ``exec-out``. Calls honour a timeout
and HighhX's cancellation token. A missing ``adb`` is a capability with the install hint, never
a crash.

Found as ``$HIGHHX_ADB``, ``adb`` on PATH, or ``$ANDROID_HOME`` / ``$ANDROID_SDK_ROOT``
``/platform-tools/adb``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.computer.providers import Capability
from highhx.core.errors import IntegrationError, OperationCancelledError, UsageError

if TYPE_CHECKING:
    from highhx.execution.cancellation import CancellationToken

Runner = Callable[[list[str], float, "CancellationToken | None"], tuple[int, bytes, bytes]]
"""``runner(argv, timeout, cancel)`` → (exit code, stdout, stderr)."""

INSTALL_HINT = "Install the Android platform tools (adb) and enable USB debugging, or start an emulator."
_PACKAGE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$")
_ACTIVITY = re.compile(r"^[\w.$/]+$")
_SERIAL = re.compile(r"^[\w.:\-\[\]]+$")
KEYCODES = {
    "home": 3,
    "back": 4,
    "call": 5,
    "endcall": 6,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "tab": 61,
    "space": 62,
    "enter": 66,
    "delete": 67,
    "backspace": 67,
    "menu": 82,
    "search": 84,
    "escape": 111,
    "forward_delete": 112,
    "app_switch": 187,
    "recents": 187,
}


class AdbError(IntegrationError):
    pass


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    """device · offline · unauthorized · …"""
    model: str = ""
    product: str = ""
    transport: str = ""
    emulator: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "device"

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "transport": self.transport,
            "emulator": self.emulator,
        }


def find_adb() -> str | None:
    override = os.environ.get("HIGHHX_ADB")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    found = shutil.which("adb")
    if found:
        return found
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            candidate = Path(root) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
            if candidate.exists():
                return str(candidate)
    return None


def subprocess_runner(argv: list[str], timeout: float, cancel: CancellationToken | None) -> tuple[int, bytes, bytes]:
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as process:
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = process.communicate(timeout=0.2)
                return process.returncode, out, err
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.cancelled:
                    process.kill()
                    process.communicate()
                    raise OperationCancelledError("adb was cancelled") from None
                if time.monotonic() > deadline:
                    process.kill()
                    process.communicate()
                    raise AdbError(f"adb timed out after {timeout:g}s: {' '.join(argv[1:4])}") from None


def escape_text(text: str) -> str:
    """``text`` for ``adb shell input text``: spaces become ``%s`` and the whole string is quoted
    for the device shell (so ``; rm …`` stays text)."""
    if "\n" in text or "\r" in text:
        raise UsageError("Android text input cannot contain line breaks; send them as key events (enter).")
    return shlex.quote(text.replace("%", r"\%").replace(" ", "%s"))


class AdbClient:
    def __init__(
        self,
        adb: str | None = None,
        *,
        serial: str | None = None,
        runner: Runner | None = None,
        cancel: CancellationToken | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.adb = adb if adb is not None else find_adb()
        if serial is not None and not _SERIAL.match(serial):
            raise UsageError(f"Invalid device serial {serial!r}.")
        self.serial = serial
        self.runner = runner or subprocess_runner
        self.cancel = cancel
        self.timeout = timeout

    # ---------------------------------------------------------------- basics
    def capability(self) -> Capability:
        if not self.adb:
            return Capability("adb", False, f"adb was not found. {INSTALL_HINT}")
        return Capability("adb", True, self.adb)

    def _argv(self, *args: str, device: bool = True) -> list[str]:
        if not self.adb:
            raise AdbError("adb was not found.", hint=INSTALL_HINT)
        base = [self.adb]
        if device and self.serial:
            base += ["-s", self.serial]
        return [*base, *args]

    def run(self, *args: str, device: bool = True, timeout: float | None = None, check: bool = True) -> bytes:
        argv = self._argv(*args, device=device)
        code, out, err = self.runner(argv, timeout or self.timeout, self.cancel)
        if check and code != 0:
            message = (err or out).decode("utf-8", "replace").strip() or f"exit code {code}"
            raise AdbError(f"adb {' '.join(args[:3])} failed: {message}", hint=_hint(message))
        return out

    def shell(self, *args: str, timeout: float | None = None) -> str:
        """A device shell command. Each argument must already be safe for the device shell."""
        out = self.run("shell", *args, timeout=timeout)
        return out.decode("utf-8", "replace")

    # --------------------------------------------------------------- devices
    def devices(self) -> list[Device]:
        text = self.run("devices", "-l", device=False).decode("utf-8", "replace")
        return parse_devices(text)

    def connect(self, address: str) -> str:
        if not re.match(r"^[\w.\-]+:\d{1,5}$", address):
            raise UsageError(f"Expected host:port, not {address!r}.")
        out = self.run("connect", address, device=False).decode("utf-8", "replace").strip()
        if "connected" not in out or "cannot" in out or "failed" in out:
            raise AdbError(f"adb could not connect to {address}: {out}")
        return out

    def require_device(self) -> Device:
        ready = [d for d in self.devices() if d.ready]
        if self.serial:
            match = next((d for d in self.devices() if d.serial == self.serial), None)
            if match is None:
                raise AdbError(f"Device {self.serial} is not connected.", hint="See `highhx android devices`.")
            if not match.ready:
                raise AdbError(f"Device {self.serial} is {match.state}.", hint=_hint(match.state))
            return match
        if not ready:
            raise AdbError("No Android device or emulator is connected.", hint=INSTALL_HINT)
        if len(ready) > 1:
            raise AdbError(
                f"{len(ready)} devices are connected; choose one.",
                hint="Pass --device SERIAL (see `highhx android devices`).",
            )
        self.serial = ready[0].serial
        return ready[0]

    # ----------------------------------------------------------------- input
    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def long_press(self, x: int, y: int, ms: int = 800) -> None:
        self.shell("input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(int(ms)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell("input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, ms)))

    def text(self, text: str) -> None:
        if text:
            self.shell("input", "text", escape_text(text))

    def keyevent(self, key: str | int) -> None:
        code = key if isinstance(key, int) else KEYCODES.get(str(key).lower())
        if code is None:
            if str(key).upper().startswith("KEYCODE_") and re.match(r"^KEYCODE_[A-Z0-9_]+$", str(key).upper()):
                self.shell("input", "keyevent", str(key).upper())
                return
            raise UsageError(f"Unknown Android key {key!r} (e.g. {', '.join(sorted(KEYCODES)[:8])} …).")
        self.shell("input", "keyevent", str(int(code)))

    # ------------------------------------------------------------ perception
    def screencap(self) -> bytes:
        data = self.run("exec-out", "screencap", "-p", timeout=30)
        if not data.startswith(b"\x89PNG"):
            data = data.replace(b"\r\n", b"\n")  # old devices translate line endings
        if not data.startswith(b"\x89PNG"):
            raise AdbError("The device did not return a PNG screenshot.")
        return data

    def ui_dump(self) -> str:
        out = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30).decode("utf-8", "replace")
        start = out.find("<?xml")
        end = out.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise AdbError(f"uiautomator returned no hierarchy: {out.strip()[:200]}")
        return out[start : end + len("</hierarchy>")]

    def screen_size(self) -> tuple[int, int] | None:
        match = re.search(r"(\d+)x(\d+)", self.shell("wm", "size"))
        return (int(match.group(1)), int(match.group(2))) if match else None

    def getprop(self, name: str) -> str:
        if not re.match(r"^[\w.]+$", name):
            raise UsageError(f"Invalid property name {name!r}.")
        return self.shell("getprop", name).strip()

    def health(self) -> dict[str, Any]:
        """Is the device usable: booted, battery, screen on, free storage, model and Android version.
        Read-only (getprop, dumpsys, df); values it cannot read are left out, never guessed."""
        out: dict[str, Any] = {"serial": self.serial or ""}
        out["booted"] = self.getprop("sys.boot_completed") == "1"
        out["android"] = self.getprop("ro.build.version.release")
        out["model"] = self.getprop("ro.product.model")
        battery = self.shell("dumpsys", "battery")
        level = re.search(r"level:\s*(\d+)", battery)
        if level:
            out["battery"] = int(level.group(1))
        power = self.shell("dumpsys", "power")
        awake = re.search(r"mWakefulness=(\w+)|Display Power: state=(\w+)", power)
        if awake:
            out["screen_on"] = (awake.group(1) or awake.group(2) or "").lower() in ("awake", "on")
        storage = self.shell("df", "/data").splitlines()
        if len(storage) >= 2:
            fields = storage[-1].split()
            if len(fields) >= 4 and fields[3].rstrip("KMGT%").isdigit():
                out["free_storage"] = fields[3]
        problems = []
        if not out["booted"]:
            problems.append("not finished booting")
        if isinstance(out.get("battery"), int) and out["battery"] < 10:
            problems.append(f"battery at {out['battery']}%")
        if out.get("screen_on") is False:
            problems.append("screen is off")
        out["healthy"] = not problems
        out["problems"] = problems
        return out

    def current_app(self) -> tuple[str, str]:
        """The focused package and activity (empty when unknown)."""
        text = self.shell("dumpsys", "window")
        match = re.search(r"mCurrentFocus=Window\{[^}]*\s([\w.]+)/([\w.$]+)\}", text) or re.search(
            r"mFocusedApp=.*?\s([\w.]+)/([\w.$]+)", text
        )
        return (match.group(1), match.group(2)) if match else ("", "")

    # ------------------------------------------------------------------ apps
    def packages(self, *, third_party: bool = False) -> list[str]:
        args = ["pm", "list", "packages", *(["-3"] if third_party else [])]
        return sorted(line.split(":", 1)[1].strip() for line in self.shell(*args).splitlines() if line.startswith("package:"))

    def launch(self, package: str, activity: str | None = None) -> None:
        check_package(package)
        if activity:
            if not _ACTIVITY.match(activity):
                raise UsageError(f"Invalid activity {activity!r}.")
            component = activity if "/" in activity else f"{package}/{activity}"
            out = self.shell("am", "start", "-n", component)
        else:
            out = self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        if "Error" in out or "No activities found" in out or "monkey aborted" in out:
            raise AdbError(f"Could not launch {package}: {out.strip()[:200]}")

    def stop(self, package: str) -> None:
        check_package(package)
        self.shell("am", "force-stop", package)

    def install(self, apk: Path) -> str:
        if not apk.is_file() or apk.suffix.lower() != ".apk":
            raise UsageError(f"{apk} is not an .apk file.")
        return self.run("install", "-r", str(apk), timeout=600).decode("utf-8", "replace").strip()

    def uninstall(self, package: str) -> str:
        check_package(package)
        return self.run("uninstall", package, timeout=120).decode("utf-8", "replace").strip()


def check_package(package: str) -> None:
    if not _PACKAGE.match(package):
        raise UsageError(f"Invalid Android package name {package!r} (e.g. com.android.settings).")


def parse_devices(text: str) -> list[Device]:
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*", "adb server")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        serial = parts[0]
        devices.append(
            Device(
                serial,
                parts[1],
                info.get("model", ""),
                info.get("product", ""),
                info.get("transport_id", ""),
                serial.startswith("emulator-"),
            )
        )
    return devices


def _hint(message: str) -> str | None:
    text = message.lower()
    if "unauthorized" in text:
        return "Accept the USB debugging prompt on the device."
    if "offline" in text:
        return "Reconnect the device or restart adb (`adb kill-server`)."
    if "no devices" in text or "not found" in text:
        return INSTALL_HINT
    return None
