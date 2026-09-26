"""Platform detection helpers.

All platform-specific branching in HighhX should go through this module so the
behaviour is easy to find and to fake in tests.
"""

from __future__ import annotations

import os
import platform as _platform
import sys

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def system_name() -> str:
    """Return a normalised operating-system name: ``windows``, ``macos``, ``linux`` or other."""
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    if IS_LINUX:
        return "linux"
    return sys.platform


def is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux."""
    if not IS_LINUX:
        return False
    return "microsoft" in _platform.release().lower()


def is_ci() -> bool:
    """Return True when a well-known CI environment variable is set."""
    markers = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TF_BUILD", "JENKINS_URL")
    return any(os.environ.get(name) for name in markers)


def executable_name(name: str) -> str:
    """Return the platform-specific file name of a wrapper script such as ``gradlew``."""
    if IS_WINDOWS and not name.lower().endswith((".bat", ".cmd", ".exe")):
        return f"{name}.bat"
    return name


def supports_posix_permissions() -> bool:
    """Return True when file mode bits are meaningful on this platform."""
    return not IS_WINDOWS
