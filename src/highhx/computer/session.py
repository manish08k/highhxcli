"""One computer-use context: providers, runtimes and application launching, behind one gate."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from highhx.computer.browser import ChromeBrowser
from highhx.computer.desktop import TesseractOCR, app_installed, launch_command, resolve_app
from highhx.computer.model import Observation
from highhx.computer.providers import Capability, ComputerUseProvider
from highhx.computer.runtime import ActionOutcome, ComputerRuntime
from highhx.core.errors import IntegrationError, NotFoundError, UsageError
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec, join_command
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs
from highhx.safety.gate import ActionGate
from highhx.utils.paths import user_data_dir

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver
    from highhx.drivers.android.adb import AdbClient

SOURCES = ("browser", "desktop")
"""Sources that can be observed *and* acted on."""
OBSERVE_SOURCES = (*SOURCES, "screen")
"""``screen`` is local OCR: read-only perception with no actionable controls."""


class ComputerSession:
    def __init__(
        self,
        gate: ActionGate,
        *,
        actor: Actor,
        tool: str = "computer",
        cancel: CancellationToken | None = None,
        state_dir: Path | None = None,
        headless: bool | None = None,
        target: str = "local",
        browser_endpoint: str = "",
    ) -> None:
        self.gate = gate
        self.actor = actor
        self.tool = tool
        self.cancel = cancel or CancellationToken()
        self.state_dir = state_dir or user_data_dir() / "computer"
        self.headless = headless
        self.target = target
        """The computer operated: ``local``, or ``ssh://user@host``."""
        self.browser_endpoint = browser_endpoint
        """An existing browser's DevTools endpoint (a remote browser); empty: HighhX's own browser."""
        self._browser: ChromeBrowser | None = None
        self._runtimes: dict[str, ComputerRuntime] = {}
        self._desktop_app: str | None = None
        self._driver: HighhXDriver | None = None
        from highhx.computer.capture import CaptureStore

        self.captures = CaptureStore()
        """Screenshots handed to models, and the checks that ground their coordinates."""

    def driver(self) -> HighhXDriver:
        """The HighhX Computer API for desktop operations — one per session, shared by HighhX Free's
        actions and HighhX Pro's agent tools, on the C#/.NET engine when installed (its newer
        operations on the built-in engine) or the built-in engine (see :mod:`highhx.automation.engine`),
        or a remote computer's engine over SSH. A connection found dead is replaced for the next
        operation — and everything observed through it (screenshots, element ids) is discarded."""
        if self._driver is not None and not self._driver.connected():
            self._driver.end_session()
            self._driver = None
            self.captures.retire("the connection to the computer was lost")
            self._runtimes.pop("desktop", None)
        if self._driver is None:
            from highhx.automation.engine.bridge import open_bridge
            from highhx.computer.driver import HighhXDriver

            engine = self.gate.engine

            def runner(argv: list[str], what: str) -> tuple[int, str, str]:
                result = engine.run(
                    CommandSpec(argv, name=argv[0], timeout=60),
                    action=what,
                    approved=True,  # the action that needs it was classified and approved
                    echo=False,
                    record=False,
                    policy_action="computer:automation",
                    cancel=self.cancel,
                )
                if result.dry_run:
                    return 0, "{}", ""
                code = 0 if result.ok else (result.exit_code or 1)
                return code, result.stdout or "", result.stderr or result.error or ""

            self._driver = HighhXDriver(open_bridge(runner, cancel=self.cancel, target=self.target), target=self.target)
        return self._driver

    def android_client(self, serial: str | None = None, cancel: CancellationToken | None = None) -> AdbClient:
        """The adb client for an Android device (a benchmark or test session supplies a simulated one)."""
        from highhx.drivers.android.adb import AdbClient

        return AdbClient(serial=serial or None, cancel=cancel or self.cancel)

    # ------------------------------------------------------------- providers
    @property
    def browser(self) -> ChromeBrowser:
        if self._browser is None:
            if self.browser_endpoint:
                from highhx.computer.browser import RemoteBrowser

                self._browser = RemoteBrowser(self.state_dir, self.browser_endpoint)
            else:
                self._browser = ChromeBrowser(self.state_dir, headless=self.headless)
        return self._browser

    def provider(self, source: str) -> ComputerUseProvider:
        if source == "browser":
            return self.browser
        if source == "desktop":  # every platform: what it cannot do, its backend says (never faked)
            from highhx.automation.engine.provider import BridgeDesktopProvider

            return BridgeDesktopProvider(self.driver(), self._desktop_app)
        raise UsageError(f"Unknown source {source!r} (expected: {', '.join(SOURCES)})")

    def runtime(self, source: str = "browser") -> ComputerRuntime:
        if source not in self._runtimes:
            self._runtimes[source] = ComputerRuntime(
                self.provider(source), self.gate, actor=self.actor, tool=self.tool, cancel=self.cancel
            )
        return self._runtimes[source]

    def observe_screen(self) -> Observation:
        """Read the screen's text with local OCR (read-only; nothing on it can be acted on)."""
        return TesseractOCR().read_screen(cancel=self.cancel)

    def capabilities(self) -> list[Capability]:
        desktop = [Capability(f.name, f.available, f.detail) for f in self.driver().capabilities().values()]
        return [
            *desktop,
            self.browser.capability(),
            TesseractOCR().capability(),
            Capability("vision", False, "no vision provider is bundled; structured perception is used instead"),
        ]

    # --------------------------------------------------------------- launch
    def launch(self, name: str) -> ActionOutcome:
        app = resolve_app(name)
        if not app_installed(name):
            raise NotFoundError(
                f"Application {app!r} was not found.", hint="Check the name, e.g. `highhx computer open Safari`."
            )
        argv = launch_command(name)
        action = ActionDescriptor(
            ActionKind.APP_LAUNCH,
            f"Launch {app}",
            self.tool,
            target=app,
            application=sys.platform,
            command=join_command(argv),
            actor=self.actor,
            attributes=attrs(app=app),
        )
        authorization = self.gate.authorize(action, policy_action="computer:launch", grant="computer:launch")
        engine = self.gate.engine
        with self.gate.executing(authorization) as event:
            result = engine.run(
                CommandSpec(argv, name="launch", timeout=30),
                action=f"Launch {app}",
                policy_action=f"exec:{argv[0]}",
                approved=True,
                echo=False,
                cancel=self.cancel,
            )
            running = self._is_running(app) if result.ok else False
            event.verified = running
            if not result.ok or running is False:
                event.status = "failed"
        self._desktop_app = app
        self._runtimes.pop("desktop", None)
        ok = result.ok and running is not False
        problems = [] if ok else [result.error or f"{app} did not start"]
        return ActionOutcome(f"launch:{app}", ok, running, action.summary, None, problems)

    def _is_running(self, app: str, timeout: float = 10.0) -> bool | None:
        """Verify the launch through the computer runtime. On macOS the application name is the
        process name; elsewhere a friendly name ("Google Chrome") need not match one, so: unknown."""
        if sys.platform != "darwin":
            return None
        driver = self.driver()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if driver.running(app):
                    return True
            except IntegrationError:
                return None  # the runtime could not tell (e.g. no such application): not a failed launch
            if time.monotonic() >= deadline or self.cancel.wait(0.3):
                return False

    def close(self) -> None:
        if self._driver is not None:
            self._driver.end_session()
            self._driver = None
        if self._browser is not None:
            self._browser.close()
