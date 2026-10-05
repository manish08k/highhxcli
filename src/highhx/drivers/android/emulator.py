"""The Android emulator: find it, list its virtual devices (AVDs), start one and stop it.

    android.emulators          list AVDs (read-only)
    android.emulator_start     start an AVD headless and wait until Android has booted
    android.emulator_stop      stop a running emulator (adb emu kill)

The emulator is started detached (its own process group, output in a log file) so it survives
the command that started it, like the HighhX browser. AndroidWorld-style evaluation expects an
emulator with gRPC enabled (``-grpc 8554``); that is the default port here.

**Status: implemented, not validated on a real emulator in this build** (no Android SDK on the
build machine); the tests drive it through a fake runner. ``highhx capabilities`` says whether an
emulator is installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from highhx.core.errors import UsageError
from highhx.drivers.android.adb import AdbClient, AdbError, Runner, subprocess_runner
from highhx.execution.cancellation import CancellationToken

INSTALL_HINT = "Install the Android SDK emulator (Android Studio → SDK Manager) and create an AVD."
_AVD = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
DEFAULT_GRPC = 8554
Spawner = Callable[[list[str], Path], int]
"""Start ``argv`` detached with output to a log file; returns the pid."""


def find_emulator() -> str | None:
    override = os.environ.get("HIGHHX_EMULATOR")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    found = shutil.which("emulator")
    if found:
        return found
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            candidate = Path(root) / "emulator" / ("emulator.exe" if os.name == "nt" else "emulator")
            if candidate.exists():
                return str(candidate)
    return None


def detached_spawner(argv: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        process = subprocess.Popen(  # a fixed emulator argv; the AVD name is validated
            argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
        )
    return process.pid


class Emulator:
    def __init__(
        self,
        emulator: str | None = None,
        *,
        adb: AdbClient | None = None,
        runner: Runner | None = None,
        spawner: Spawner | None = None,
        cancel: CancellationToken | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.emulator = emulator if emulator is not None else find_emulator()
        self.runner = runner or subprocess_runner
        self.adb = adb or AdbClient(runner=runner, cancel=cancel)
        self.spawner = spawner or detached_spawner
        self.cancel = cancel
        self.sleep = sleep

    def _require(self) -> str:
        if not self.emulator:
            raise AdbError("The Android emulator was not found.", hint=INSTALL_HINT)
        return self.emulator

    def avds(self) -> list[str]:
        code, out, err = self.runner([self._require(), "-list-avds"], 30.0, self.cancel)
        if code != 0:
            raise AdbError(f"emulator -list-avds failed: {(err or out).decode('utf-8', 'replace').strip()[:200]}")
        return [line.strip() for line in out.decode("utf-8", "replace").splitlines() if _AVD.match(line.strip())]

    def start(
        self,
        avd: str,
        *,
        log: Path,
        grpc_port: int = DEFAULT_GRPC,
        headless: bool = True,
        wipe: bool = False,
        timeout: float = 300.0,
    ) -> dict[str, object]:
        """Start ``avd`` and wait until ``sys.boot_completed`` is 1 on its new serial."""
        if not _AVD.match(avd):
            raise UsageError(f"Invalid AVD name {avd!r}.")
        known = self.avds()
        if avd not in known:
            raise UsageError(f"No AVD named {avd!r}.", hint=f"Known: {', '.join(known) or 'none'} ({INSTALL_HINT})")
        before = {d.serial for d in self.adb.devices() if d.emulator}
        argv = [self._require(), "-avd", avd, "-grpc", str(int(grpc_port)), "-no-snapshot-save"]
        if headless:
            argv += ["-no-window", "-no-audio"]
        if wipe:
            argv.append("-wipe-data")
        pid = self.spawner(argv, log)
        deadline = time.monotonic() + timeout
        serial = ""
        while time.monotonic() < deadline:
            if self.cancel is not None and self.cancel.cancelled:
                raise AdbError(
                    "Cancelled while the emulator was starting (it keeps running; stop it with android.emulator_stop)."
                )
            if not serial:
                new = [d for d in self.adb.devices() if d.emulator and d.serial not in before]
                serial = new[0].serial if new else ""
            if serial:
                device = AdbClient(self.adb.adb, serial=serial, runner=self.runner, cancel=self.cancel)
                booted = device.run("shell", "getprop", "sys.boot_completed", timeout=10.0, check=False)
                if booted.decode("utf-8", "replace").strip() == "1":
                    return {"avd": avd, "serial": serial, "pid": pid, "grpc_port": grpc_port, "log": str(log)}
            self.sleep(2.0)
        raise AdbError(
            f"{avd} did not finish booting within {timeout:g}s" + (f" (serial {serial})" if serial else ""),
            hint=f"See {log}.",
        )

    def reset(self, serial: str, avd: str, *, log: Path, timeout: float = 300.0) -> dict[str, object]:
        """A factory reset: stop the emulator, then start the AVD again with its data wiped."""
        self.stop(serial)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(d.serial == serial for d in self.adb.devices()):
            self.sleep(1.0)
        return self.start(avd, log=log, wipe=True, timeout=timeout)

    def stop(self, serial: str) -> None:
        if not serial.startswith("emulator-"):
            raise UsageError(f"{serial!r} is not an emulator.")
        AdbClient(self.adb.adb, serial=serial, runner=self.runner, cancel=self.cancel).run("emu", "kill")
