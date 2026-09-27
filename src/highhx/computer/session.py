"""One computer-use context: providers, runtimes and application launching, behind one gate."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from highhx.computer.browser import ChromeBrowser
from highhx.computer.desktop import MacAccessibility, TesseractOCR, app_installed, launch_command, resolve_app
from highhx.computer.model import Observation
from highhx.computer.providers import Capability, ComputerUseProvider
from highhx.computer.runtime import ActionOutcome, ComputerRuntime
from highhx.core.errors import NotFoundError, UsageError
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec, join_command
from highhx.safety.actions import ActionDescriptor, ActionKind, Actor, attrs
from highhx.safety.gate import ActionGate
from highhx.utils.paths import user_data_dir

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
    ) -> None:
        self.gate = gate
        self.actor = actor
        self.tool = tool
        self.cancel = cancel or CancellationToken()
        self.state_dir = state_dir or user_data_dir() / "computer"
        self.headless = headless
        self._browser: ChromeBrowser | None = None
        self._runtimes: dict[str, ComputerRuntime] = {}
        self._desktop_app: str | None = None

    # ------------------------------------------------------------- providers
    @property
    def browser(self) -> ChromeBrowser:
        if self._browser is None:
            self._browser = ChromeBrowser(self.state_dir, headless=self.headless)
        return self._browser

    def provider(self, source: str) -> ComputerUseProvider:
        if source == "browser":
            return self.browser
        if source == "desktop":
            if sys.platform != "darwin":
                raise UsageError(
                    "Native desktop automation is only implemented on macOS.",
                    hint="Use browser automation (`--source browser`) on this platform.",
                )
            return MacAccessibility(self._desktop_app)
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
        return [
            MacAccessibility().capability(),
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
        """Verify the launch. macOS answers without Accessibility permission; elsewhere unknown."""
        if sys.platform != "darwin":
            return None
        engine = self.gate.engine
        script = f"Application({json.dumps(app)}).running()"
        deadline = time.monotonic() + timeout
        while True:
            result = engine.capture(CommandSpec(["osascript", "-l", "JavaScript", "-e", script], timeout=10))
            if result.ok and result.stdout.strip() == "true":
                return True
            if time.monotonic() >= deadline or self.cancel.wait(0.3):
                return False

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
