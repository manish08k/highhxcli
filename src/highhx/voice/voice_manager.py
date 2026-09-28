"""Voice readiness and first-run setup.

``status()`` only *detects* (fast, nothing installed, microphone untouched). ``setup()`` fixes
what is missing, asking first: install the recorder and whisper.cpp, download and verify the
model, check the microphone. What it finds is saved in :class:`VoiceConfig`, so the next
``/voice on`` starts immediately.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from rich.markup import escape

from highhx.voice.config import VoiceConfig
from highhx.voice.engines import Recorder, Speaker, VoiceEngines, VoiceError
from highhx.voice.installer import Installer
from highhx.voice.model_manager import ModelManager
from highhx.voice.platform import VoicePlatform, Which
from highhx.voice.platform import detect as detect_platform
from highhx.voice.recorder import AudioLevel, find_recorders, record_for
from highhx.voice.speech_output import choose_speaker
from highhx.voice.whisper import WhisperCppTranscriber, find_binary, verify_binary

MICROPHONE_CHECK_SECONDS = 1.0


class SetupUI(Protocol):
    def print(self, renderable: Any = "") -> None: ...

    def ask(self, question: str, *, default: bool = False) -> bool: ...


@dataclass(frozen=True)
class Component:
    name: str
    ok: bool
    detail: str
    fix: str = ""


@dataclass
class VoiceStatus:
    platform: VoicePlatform
    recorder: str | None
    whisper: str | None
    model: str
    model_state: str
    microphone: str
    speaker: str | None
    replies: bool
    mode: str
    components: list[Component] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.components)

    @property
    def missing(self) -> list[Component]:
        return [c for c in self.components if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "platform": self.platform.label,
            "stt": "whisper.cpp",
            "whisper": self.whisper,
            "model": self.model,
            "model_state": self.model_state,
            "recorder": self.recorder,
            "microphone": self.microphone,
            "speech": self.speaker,
            "replies": self.replies,
            "mode": self.mode,
            "missing": [{"component": c.name, "problem": c.detail, "fix": c.fix} for c in self.missing],
        }


class VoiceManager:
    def __init__(
        self,
        config: VoiceConfig | None = None,
        *,
        platform: VoicePlatform | None = None,
        which: Which = shutil.which,
        installer: Installer | None = None,
        models: ModelManager | None = None,
        recorders: Callable[[], list[Recorder]] | None = None,
        speaker: Callable[[], Speaker | None] | None = None,
        microphone_check: Callable[[Recorder, float], AudioLevel] = record_for,  # type: ignore[assignment]
        binary_check: Callable[[str], bool] = verify_binary,
        persist: bool = True,
    ) -> None:
        self.config = config or VoiceConfig.load()
        self.platform = platform or detect_platform(which)
        self.which = which
        self.installer = installer or Installer(self.platform, which=which)
        self.models = models or ModelManager(self.config)
        self._recorders = recorders or (lambda: list(find_recorders(self.platform, which)))
        self._speaker = speaker or (lambda: choose_speaker(which))
        self._microphone_check = microphone_check
        self._binary_check = binary_check
        self.persist = persist

    def save(self) -> None:
        if self.persist:
            self.config.save()

    # ------------------------------------------------------------- detection
    def recorder(self) -> Recorder | None:
        recorders = self._recorders()
        preferred = next((r for r in recorders if r.name == self.config.recorder), None)
        return preferred or (recorders[0] if recorders else None)

    def whisper_binary(self) -> str | None:
        return find_binary(self.config, self.which)

    def status(self) -> VoiceStatus:
        recorder = self.recorder()
        binary = self.whisper_binary() if self.platform.supported else None
        model_state = self.models.status()
        speaker = self._speaker()
        status = VoiceStatus(
            platform=self.platform,
            recorder=recorder.name if recorder else None,
            whisper=binary,
            model=self.models.name,
            model_state=model_state,
            microphone=self.config.microphone,
            speaker=speaker.name if speaker else None,
            replies=self.config.replies,
            mode=self.config.mode,
        )
        reason = self.platform.unsupported_reason()
        if reason:
            status.components.append(Component("Platform", False, reason, ""))
            return status
        status.components += [
            Component("Recorder", recorder is not None, recorder.name if recorder else "not installed", "/voice setup"),
            Component("whisper.cpp", binary is not None, binary or "not installed", "/voice setup"),
            Component(
                "Model",
                model_state == "ready",
                f"{self.models.name} ({model_state})",
                "/voice setup",
            ),
        ]
        if self.config.microphone in ("denied", "silent"):
            status.components.append(
                Component("Microphone", False, "access denied", "allow microphone access, then /voice setup")
            )
        return status

    def engines(self) -> VoiceEngines:
        """Ready engines (no setup): listening needs recorder, whisper.cpp and a verified model."""
        engines = VoiceEngines(speaker=self._speaker(), model=self.models.name)
        status = self.status()
        engines.notes = [f"{c.name}: {c.detail} — run {c.fix}" if c.fix else c.detail for c in status.missing]
        if not self.platform.supported:
            return engines
        engines.recorder = self.recorder()
        binary = status.whisper
        if binary and status.model_state == "ready":
            try:
                model = self.models.verify()
            except VoiceError as exc:
                engines.notes.append(exc.message)
            else:
                engines.transcriber = WhisperCppTranscriber(binary, model, language=self.config.language)
        self.save()  # a first full checksum is remembered
        return engines

    # ----------------------------------------------------------------- setup
    def setup(self, ui: SetupUI, *, assume_yes: bool = False, check_microphone: bool = True) -> VoiceStatus:
        """Install, download and check what is missing — asking first. Never raises."""

        def agree(question: str) -> bool:
            if assume_yes:
                return True
            if not getattr(ui, "interactive", True):
                return False  # nothing is installed or downloaded without a person agreeing
            return ui.ask(question, default=True)

        reason = self.platform.unsupported_reason()
        if reason:
            ui.print(f"[warn]⚠ {escape(reason)}[/warn]")
            return self.status()
        try:
            if not self._setup_programs(ui, agree):
                return self.status()
            if not self._setup_model(ui, agree):
                return self.status()
            if check_microphone and self.config.microphone != "ready":
                self.check_microphone(ui, agree)
        except VoiceError as exc:
            report(ui, exc)
        finally:
            self.save()
        return self.status()

    def _setup_programs(self, ui: SetupUI, agree: Callable[[str], bool]) -> bool:
        recorder = self.recorder()
        binary = self.whisper_binary()
        if binary and binary != self.config.whisper_binary:
            self.config.whisper_binary = binary
        if recorder is not None and binary is not None:
            return True
        ui.print("\n[bold magenta]🎙 HighhX Voice setup[/bold magenta]")
        steps: list[tuple[str, Callable[[], None]]] = []
        if recorder is None:
            plan = self.installer.recorder_plan()
            if plan is None:
                self.installer.install_recorder(ui.print)  # raises with the recovery path
            ui.print("An audio recorder is not installed.")
            steps.append((plan or "", lambda: self.installer.install_recorder(_log(ui))))
        if binary is None:
            plan = self.installer.whisper_plan()
            ui.print("Whisper.cpp is not installed.")
            if plan is None:
                self.installer.install_whisper(_log(ui))  # raises with the recovery path
            steps.append((plan or "", self._install_whisper(ui)))
        for plan, _ in steps:
            ui.print(f"  [dim]→ {escape(plan)}[/dim]")
        if not agree("Set up local voice now?"):
            ui.print(
                "[dim]Voice setup skipped — HighhX keeps working normally. Run /voice on (or /voice setup) "
                "whenever you want local voice.[/dim]"
            )
            return False
        for _, step in steps:
            step()
        recorder = self.recorder()
        if recorder is None:
            raise VoiceError("recorder-unavailable", "The audio recorder is still unavailable after installing.")
        self.config.recorder = recorder.name
        ui.print(f"[ok]✓[/ok] Recorder: {escape(recorder.name)}")
        return True

    def _install_whisper(self, ui: SetupUI) -> Callable[[], None]:
        def install() -> None:
            binary = self.installer.install_whisper(_log(ui))
            if not self._binary_check(binary):
                raise VoiceError(
                    "whisper-unavailable",
                    "whisper.cpp was installed but does not run.",
                    hint="Run /voice setup again, or reinstall with `brew reinstall whisper-cpp`.",
                )
            self.config.whisper_binary = binary
            ui.print(f"[ok]✓[/ok] whisper.cpp: {escape(binary)}")

        return install

    def _setup_model(self, ui: SetupUI, agree: Callable[[str], bool]) -> bool:
        state = self.models.status()
        if state == "ready":
            try:
                self.models.verify()
                return True
            except VoiceError as exc:
                state = "corrupted" if exc.problem == "model-corrupted" else "missing"
        info = self.models.info
        ui.print("Whisper model is damaged." if state == "corrupted" else "Whisper model not found.")
        if not agree(f"Download required voice model ({info.name}, {info.size_label})?"):
            ui.print("[dim]Model download skipped — run /voice setup when you are ready.[/dim]")
            return False
        with DownloadProgress(ui, info.name) as progress:
            self.models.download(progress)
        ui.print(f"[ok]✓[/ok] Model {escape(info.name)} downloaded and verified (SHA-256)")
        return True

    def check_microphone(self, ui: SetupUI, agree: Callable[[str], bool] | None = None) -> bool:
        """Record one second to see whether the microphone delivers audio; offer retries."""
        agree = agree or (lambda q: ui.ask(q, default=True))
        recorder = self.recorder()
        if recorder is None:
            return False
        while True:
            ui.print(f"[dim]Checking the microphone ({MICROPHONE_CHECK_SECONDS:.0f} second)…[/dim]")
            try:
                self._microphone_check(recorder, MICROPHONE_CHECK_SECONDS)
            except VoiceError as exc:
                self.config.microphone = "denied"
                self.save()
                report(ui, exc)
                if not agree("Retry the microphone?"):
                    return False
                continue
            self.config.microphone = "ready"
            self.config.recorder = recorder.name
            self.save()
            return True

    def record_microphone(self, ok: bool) -> None:
        """What a real recording showed about the microphone."""
        state = "ready" if ok else "denied"
        if self.config.microphone != state:
            self.config.microphone = state
            self.save()


def report(ui: SetupUI, exc: VoiceError) -> None:
    ui.print(f"[warn]⚠ {escape(exc.message)}[/warn]")
    if exc.hint:
        ui.print(f"\n{escape(exc.hint)}")


def _log(ui: SetupUI) -> Callable[[str], None]:
    return lambda line: ui.print(f"  [dim]│ {escape(line[-200:])}[/dim]")


class DownloadProgress:
    """A progress bar on a terminal console; otherwise a line every 25%."""

    def __init__(self, ui: SetupUI, name: str) -> None:
        self.ui = ui
        self.name = name
        self._bar: Any = None
        self._task: Any = None
        self._shown = -1

    def __enter__(self) -> Callable[[int, int], None]:
        console = getattr(ui_console := getattr(self.ui, "console", None), "is_terminal", False) and ui_console
        if console:
            from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn, TransferSpeedColumn

            self._bar = Progress(
                f"  Downloading {self.name}",
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            )
            self._bar.start()
            self._task = self._bar.add_task("download", total=None)
        return self.update

    def update(self, done: int, total: int) -> None:
        if self._bar is not None:
            self._bar.update(self._task, completed=done, total=total)
            return
        quarter = int(done * 4 / total) if total else 0
        if quarter > self._shown:
            self._shown = quarter
            self.ui.print(f"  [dim]Downloading {escape(self.name)}: {quarter * 25}%[/dim]")

    def __exit__(self, *_exc: object) -> None:
        if self._bar is not None:
            self._bar.stop()
