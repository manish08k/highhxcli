"""What HighhX can do on this computer, right now — detected, not claimed.

Every entry is decided by looking (a binary on PATH, the platform, a configured endpoint), never
by contacting a model or starting a browser. ``available`` means the prerequisite is present; it
does not mean the capability was exercised here — that is what the opt-in real-platform tests
are for (README, "Real-platform testing").

    status   available · unavailable (a tool or platform is missing) · not configured ·
             requires HighhX Pro (not checked here: that would contact the platform) · not implemented
    experimental  implemented, but not validated on a real platform in this build
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.commands import App

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
NOT_CONFIGURED = "not configured"
NOT_IMPLEMENTED = "not implemented"
REQUIRES_PLAN = "requires HighhX Pro"


@dataclass(frozen=True)
class CapabilityEntry:
    area: str
    name: str
    status: str
    detail: str
    requires: str = ""
    """The optional dependency or configuration it needs ("" when built in)."""
    experimental: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _which(name: str) -> str:
    return shutil.which(name) or ""


def capability_report(app: App | None = None) -> list[CapabilityEntry]:
    from highhx.computer.browser import find_browser
    from highhx.drivers.android.adb import find_adb
    from highhx.runtimes.sandbox import available_backends

    out: list[CapabilityEntry] = []
    add = out.append

    browser = find_browser()
    found = str(browser) if browser else ""
    add(
        CapabilityEntry(
            "browser",
            "automation (DOM + accessibility, CDP)",
            AVAILABLE if found else UNAVAILABLE,
            found or "no Chrome, Chromium, Edge or Brave found",
            "a Chromium-family browser",
        )
    )
    for name in (
        "CDP (DevTools)",
        "network evidence and network waits",
        "schema extraction",
        "recording and replay",
        "visual click (click_at)",
        "profiles and managed sessions",
        "live viewing (screencast)",
    ):
        add(
            CapabilityEntry(
                "browser",
                name,
                AVAILABLE if found else UNAVAILABLE,
                "built into the HighhX browser" if found else "needs the HighhX browser",
                "a Chromium-family browser",
            )
        )
    remote = os.environ.get("HIGHHX_BROWSER_ENDPOINT", "")
    add(
        CapabilityEntry(
            "browser",
            "remote browser (wss:// DevTools)",
            AVAILABLE if remote else NOT_CONFIGURED,
            "configured" if remote else "connect with an explicit wss:// endpoint or an ssh -L tunnel",
            "a remote browser you control",
        )
    )

    platform = {"darwin": "macOS", "win32": "Windows"}.get(
        sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform
    )
    desktop_note = {
        "macOS": "Accessibility and Screen Recording permissions are checked when used (`highhx computer status`)",
        "Windows": "UI Automation; not validated in this build",
        "Linux": "AT-SPI/X11; not validated in this build",
    }.get(platform, "no desktop backend for this platform")
    add(
        CapabilityEntry(
            "desktop",
            f"desktop control ({platform})",
            AVAILABLE if platform in ("macOS", "Windows", "Linux") else UNAVAILABLE,
            desktop_note,
            experimental=platform != "macOS",
        )
    )
    tesseract = _which("tesseract")
    add(
        CapabilityEntry(
            "perception",
            "OCR",
            AVAILABLE if tesseract else UNAVAILABLE,
            tesseract or "tesseract is not installed (brew install tesseract / apt install tesseract-ocr)",
            "tesseract",
        )
    )

    adb = find_adb() or ""
    emulator = _which("emulator")
    add(
        CapabilityEntry(
            "android",
            "devices over adb",
            AVAILABLE if adb else UNAVAILABLE,
            adb or "adb is not installed (Android platform tools)",
            "adb",
            experimental=True,
        )
    )
    add(
        CapabilityEntry(
            "android",
            "emulator",
            AVAILABLE if emulator else UNAVAILABLE,
            emulator or "the Android emulator is not installed (Android SDK)",
            "Android SDK emulator",
            experimental=True,
        )
    )

    for name, cap in available_backends().items():
        if name == "workspace":
            add(CapabilityEntry("sandbox", "workspace copy (no confinement)", AVAILABLE, cap.detail))
            continue
        add(
            CapabilityEntry(
                "sandbox",
                name,
                AVAILABLE if cap.available else UNAVAILABLE,
                cap.detail,
                {"seatbelt": "macOS sandbox-exec", "bubblewrap": "bwrap (Linux)", "docker": "Docker"}[name],
                experimental=name != "seatbelt",
            )
        )
    from highhx.runtimes.vm import available_vm_backends

    for name, cap in available_vm_backends().items():
        add(
            CapabilityEntry(
                "vm",
                f"virtual machines ({name})",
                AVAILABLE if cap.available else UNAVAILABLE,
                cap.detail,
                {"lima": "Lima (limactl)", "tart": "Tart (Apple silicon)"}[name],
                experimental=True,
            )
        )
    ssh = _which("ssh")
    target = os.environ.get("HIGHHX_COMPUTER_TARGET", "")
    add(
        CapabilityEntry(
            "remote",
            "remote computer (ssh://, keys only)",
            (AVAILABLE if target else NOT_CONFIGURED) if ssh else UNAVAILABLE,
            ("configured" if target else "set HIGHHX_COMPUTER_TARGET=ssh://user@host")
            if ssh
            else "ssh is not installed",
            "ssh and HighhX on the other computer",
            experimental=True,
        )
    )

    vision: dict[str, Any] = {}
    if app is not None:
        from highhx.computer.operator.models import vision_config

        vision = vision_config(app)
    ollama = _which("ollama")
    add(
        CapabilityEntry(
            "models",
            "Ollama",
            AVAILABLE if ollama else UNAVAILABLE,
            (ollama + " (installed; `highhx agent models --discover` lists its models)")
            if ollama
            else "not installed (ollama.com); any OpenAI-compatible local server works too",
            "Ollama or another local model server",
        )
    )
    planner_local = bool(os.environ.get("HIGHHX_PLANNER_BASE_URL") and os.environ.get("HIGHHX_PLANNER_MODEL"))
    add(
        CapabilityEntry(
            "models",
            "local planner model (OpenAI-compatible)",
            AVAILABLE if planner_local else NOT_CONFIGURED,
            "configured (not contacted)" if planner_local else "set HIGHHX_PLANNER_BASE_URL and HIGHHX_PLANNER_MODEL",
            "a model served locally (Ollama, llama.cpp, vLLM …)",
        )
    )
    local_vision = vision.get("provider") == "local" and bool(vision.get("base_url"))
    add(
        CapabilityEntry(
            "models",
            "local vision / grounding model",
            AVAILABLE if local_vision else NOT_CONFIGURED,
            "configured (not contacted)" if local_vision else "set HIGHHX_VISION_BASE_URL and HIGHHX_VISION_MODEL",
            "a vision model served locally",
        )
    )
    add(
        CapabilityEntry(
            "models",
            "remote models",
            REQUIRES_PLAN,
            "HighhX Pro, through the HighhX platform (no provider key on this machine; see `highhx account`); "
            "each use needs --remote-model consent",
            "HighhX Pro",
        )
    )

    for name in (
        "MCP client and server",
        "workflows (for_each, while, choose, wait, handoff, verify, rollback, resume)",
        "trajectories, search and reproducibility metadata",
        "task traces, events and timeline",
        "benchmarks (8 metrics + diagnostics)",
        "web console (highhx web)",
        "approvals (approve, reject, modify, defer, timeout)",
        "project memory",
        "application skills",
        "task artifacts",
        "multi-agent supervisor with budgets",
        "best-of-N on project copies",
    ):
        add(CapabilityEntry("platform", name, AVAILABLE, "built in"))
    pdftotext = _which("pdftotext")
    add(
        CapabilityEntry(
            "files",
            "PDF text (filesystem.parse)",
            AVAILABLE if pdftotext else UNAVAILABLE,
            pdftotext or "pdftotext is not installed (poppler)",
            "poppler",
        )
    )
    smtp = os.environ.get("HIGHHX_SMTP_HOST", "")
    add(
        CapabilityEntry(
            "workflows",
            "e-mail (email.send)",
            AVAILABLE if smtp else NOT_CONFIGURED,
            f"SMTP {smtp}" if smtp else "set HIGHHX_SMTP_HOST and friends",
            "an SMTP server",
        )
    )
    return out


def summary(entries: list[CapabilityEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts
