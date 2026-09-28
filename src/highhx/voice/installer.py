"""Installing voice dependencies — only after the person agreed to it.

* macOS: Homebrew (``whisper-cpp`` and ``sox`` for the recorder).
* Linux: Homebrew when present, otherwise the system package manager for the recorder
  (``alsa-utils``) and a pinned whisper.cpp release built from source into HighhX's data
  directory (needs git, cmake and a C++ compiler).

Every command's output is streamed to ``log`` so the person sees what is happening.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed argument lists for package managers and cmake; no shell
from collections.abc import Callable
from pathlib import Path

from highhx.voice.engines import VoiceError
from highhx.voice.platform import VoicePlatform, Which
from highhx.voice.whisper import KNOWN_LOCATIONS, managed_binary

WHISPER_REPO = "https://github.com/ggml-org/whisper.cpp"
WHISPER_TAG = "v1.9.4"

Log = Callable[[str], None]
Runner = Callable[[list[str], Log], int]


def run_streaming(argv: list[str], log: Log) -> int:
    try:
        process = subprocess.Popen(  # nosec B603 - fixed argv
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True
        )
    except OSError as exc:
        log(str(exc))
        return 127
    assert process.stdout is not None
    for line in process.stdout:
        if line.strip():
            log(line.rstrip())
    return process.wait()


RECORDER_PACKAGES = {
    "brew": "sox",
    "apt-get": "alsa-utils",
    "dnf": "alsa-utils",
    "pacman": "alsa-utils",
    "zypper": "alsa-utils",
}


def _package_command(platform: VoicePlatform, package: str) -> list[str]:
    manager = platform.package_manager
    sudo = ["sudo"] if platform.needs_root else []
    if manager == "brew":
        return ["brew", "install", package]
    if manager == "apt-get":
        return [*sudo, "apt-get", "install", "-y", package]
    if manager == "dnf":
        return [*sudo, "dnf", "install", "-y", package]
    if manager == "pacman":
        return [*sudo, "pacman", "-S", "--noconfirm", package]
    return [*sudo, "zypper", "--non-interactive", "install", package]


class Installer:
    def __init__(self, platform: VoicePlatform, *, which: Which = shutil.which, runner: Runner = run_streaming) -> None:
        self.platform = platform
        self.which = which
        self.runner = runner

    # ------------------------------------------------------------ recorder
    def recorder_plan(self) -> str | None:
        """How the recorder would be installed, or None when HighhX cannot install one here."""
        if self.platform.package_manager is None:
            return None
        return " ".join(_package_command(self.platform, RECORDER_PACKAGES[self.platform.package_manager]))

    def install_recorder(self, log: Log) -> None:
        if self.platform.package_manager is None:
            raise VoiceError(
                "recorder-unavailable",
                "No audio recorder is available and HighhX cannot install one here.",
                hint="Install Homebrew (https://brew.sh) and run /voice setup again."
                if self.platform.system == "macos"
                else "Install `sox` or `alsa-utils` with your package manager, then run /voice setup.",
            )
        package = RECORDER_PACKAGES[self.platform.package_manager]
        if self.runner(_package_command(self.platform, package), log) != 0:
            raise VoiceError(
                "recorder-unavailable",
                f"Installing the audio recorder ({package}) failed.",
                hint="See the output above.",
            )

    # ------------------------------------------------------------- whisper
    def whisper_plan(self) -> str | None:
        if self.platform.package_manager == "brew":
            return "brew install whisper-cpp"
        if self.platform.system == "linux" and self._can_build():
            return f"build whisper.cpp {WHISPER_TAG} from source into HighhX's data directory"
        return None

    def _can_build(self) -> bool:
        return all(self.which(tool) for tool in ("git", "cmake")) and bool(self.which("c++") or self.which("g++"))

    def install_whisper(self, log: Log) -> str:
        """Install whisper.cpp; returns the path of its CLI."""
        if self.platform.package_manager == "brew":
            if self.runner(["brew", "install", "whisper-cpp"], log) != 0:
                raise VoiceError(
                    "whisper-unavailable", "Installing whisper.cpp with Homebrew failed.", hint="See the output above."
                )
            found = self.which("whisper-cli") or next(
                (p for p in KNOWN_LOCATIONS if Path(p).is_file()),
                None,
            )
            if not found:
                raise VoiceError("whisper-unavailable", "whisper.cpp installed, but whisper-cli was not found.")
            return found
        if self.platform.system == "linux" and self._can_build():
            return self._build(log)
        raise VoiceError(
            "whisper-unavailable",
            "HighhX cannot install whisper.cpp on this machine automatically.",
            hint="Install Homebrew (https://brew.sh), or git, cmake and a C++ compiler, then run /voice setup.",
        )

    def _build(self, log: Log) -> str:
        binary = managed_binary()
        source = binary.parents[2]
        if not (source / "CMakeLists.txt").is_file():
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                shutil.rmtree(source)
            clone = ["git", "clone", "--depth", "1", "--branch", WHISPER_TAG, WHISPER_REPO, str(source)]
            if self.runner(clone, log) != 0:
                raise VoiceError(
                    "whisper-unavailable", "Downloading whisper.cpp failed.", hint="Check your connection."
                )
        steps = [
            [
                "cmake",
                "-S",
                str(source),
                "-B",
                str(source / "build"),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=OFF",
            ],
            ["cmake", "--build", str(source / "build"), "--config", "Release", "-j", "--target", "whisper-cli"],
        ]
        for step in steps:
            if self.runner(step, log) != 0:
                raise VoiceError("whisper-unavailable", "Building whisper.cpp failed.", hint="See the output above.")
        if not binary.is_file():
            raise VoiceError("whisper-unavailable", "whisper.cpp built, but whisper-cli was not produced.")
        return str(binary)
