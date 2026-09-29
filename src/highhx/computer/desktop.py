"""Native desktop automation: application launch, macOS Accessibility, and OCR.

* Applications are launched with the platform's own launcher (``open -a``,
  ``gtk-launch`` / ``xdg-open``, ``start``) — never through a shell.
* :class:`MacAccessibility` reads the frontmost window's accessibility tree through
  System Events (JavaScript for Automation) and presses / sets values on elements.
  It needs the Accessibility permission for the terminal
  (System Settings → Privacy & Security → Accessibility).
* Windows UI Automation and Linux AT-SPI are not implemented; the capability report
  says so instead of pretending.
* :class:`TesseractOCR` reads screen text with a local ``tesseract`` binary.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # nosec B404 - subprocess used with fixed argv only
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from highhx.computer.model import Observation, UIElement
from highhx.computer.providers import Capability
from highhx.core.errors import IntegrationError, OperationCancelledError, ToolNotFoundError
from highhx.execution.cancellation import CancellationToken

KNOWN_APPS = {
    "chrome": {"darwin": "Google Chrome", "linux": "google-chrome", "win32": "chrome"},
    "google chrome": {"darwin": "Google Chrome", "linux": "google-chrome", "win32": "chrome"},
    "safari": {"darwin": "Safari"},
    "firefox": {"darwin": "Firefox", "linux": "firefox", "win32": "firefox"},
    "edge": {"darwin": "Microsoft Edge", "linux": "microsoft-edge", "win32": "msedge"},
    "terminal": {"darwin": "Terminal", "linux": "x-terminal-emulator", "win32": "wt"},
    "vscode": {"darwin": "Visual Studio Code", "linux": "code", "win32": "code"},
    "vs code": {"darwin": "Visual Studio Code", "linux": "code", "win32": "code"},
    "finder": {"darwin": "Finder"},
    "slack": {"darwin": "Slack", "linux": "slack", "win32": "slack"},
    "notes": {"darwin": "Notes"},
    "calculator": {"darwin": "Calculator", "linux": "gnome-calculator", "win32": "calc"},
}


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


def resolve_app(name: str) -> str:
    """Map a friendly name ("chrome", "vs code") to the platform's application name."""
    entry = KNOWN_APPS.get(name.strip().lower())
    if entry:
        found = entry.get(_platform_key())
        if found:
            return found
    return name.strip()


def launch_command(name: str) -> list[str]:
    app = resolve_app(name)
    key = _platform_key()
    if key == "darwin":
        return ["open", "-a", app]
    if key == "win32":
        return ["cmd", "/c", "start", "", app]
    if shutil.which(app):
        return [app]
    if shutil.which("gtk-launch"):
        return ["gtk-launch", app]
    raise ToolNotFoundError(app, purpose="launch the application")


def mac_app_dirs() -> tuple[str, ...]:
    """Where macOS applications live (Terminal and Activity Monitor are in Utilities)."""
    return (
        "/Applications",
        "/Applications/Utilities",
        "/System/Applications",
        "/System/Applications/Utilities",
        str(Path.home() / "Applications"),
    )


def app_installed(name: str) -> bool:
    app = resolve_app(name)
    key = _platform_key()
    if key == "darwin":
        return any(Path(base, f"{app}.app").exists() for base in mac_app_dirs())
    if key == "win32":
        return True  # `start` resolves registered applications; failures are reported when launching
    return bool(shutil.which(app))


def run_cancellable(
    argv: list[str], *, timeout: float, cancel: CancellationToken | None, what: str
) -> tuple[int, str, str]:
    """Run a fixed argv (no shell), killing it — and anything it started — on cancellation or timeout."""
    posix = os.name == "posix"
    process = subprocess.Popen(  # nosec B603 - fixed argv, no shell; callers pass well-known system tools
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=posix,
    )

    def stop() -> None:
        try:
            if posix:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if cancel is not None and cancel.cancelled:
            stop()
            raise OperationCancelledError(f"{what} cancelled.")
        if time.monotonic() > deadline:
            stop()
            raise IntegrationError(f"{what} did not respond in time.")
        time.sleep(0.05)
    out, err = process.communicate()
    return process.returncode, out, err


# ------------------------------------------------------------------ macOS AX
_AX_OBSERVE = r"""
function run(argv) {
  const se = Application('System Events');
  const procs = argv[0] ? se.processes.whose({name: argv[0]}) : se.processes.whose({frontmost: true});
  if (procs.length === 0) return JSON.stringify({error: 'application not running'});
  const proc = procs[0];
  const out = [];
  const roles = {AXButton: 'button', AXTextField: 'textbox', AXTextArea: 'textbox', AXSecureTextField: 'textbox',
                 AXCheckBox: 'checkbox', AXRadioButton: 'radio', AXPopUpButton: 'combobox', AXComboBox: 'combobox',
                 AXMenuItem: 'menuitem', AXMenuButton: 'button', AXLink: 'link', AXTabGroup: 'tab', AXSlider: 'slider',
                 AXSearchField: 'searchbox', AXStaticText: 'text', AXHeading: 'heading'};
  let title = '';
  let items = [];
  try { const w = proc.windows[0]; title = w.name() || ''; items = w.entireContents(); }
  catch (e) { return JSON.stringify({app: proc.name(), title, error: String(e)}); }
  for (let i = 0; i < items.length && out.length < 400; i++) {
    const el = items[i];
    let r = ''; try { r = el.role(); } catch (e) { continue; }
    const role = roles[r]; if (!role) continue;
    const get = f => { try { const v = el[f](); return v === null || v === undefined ? '' : String(v); } catch (e) { return ''; } };
    const secure = r === 'AXSecureTextField';
    let bounds = null;
    try { const p = el.position(), z = el.size(); bounds = [p[0], p[1], z[0], z[1]]; } catch (e) {}
    out.push({index: i, role, name: get('name') || get('description') || get('title'),
              value: secure ? '' : get('value').slice(0, 300), enabled: get('enabled') !== 'false',
              focused: get('focused') === 'true', secure, bounds});
  }
  return JSON.stringify({app: proc.name(), title, elements: out});
}
"""

_AX_ACT = r"""
function run(argv) {
  const [appName, index, action, text] = argv;
  const se = Application('System Events');
  const procs = appName ? se.processes.whose({name: appName}) : se.processes.whose({frontmost: true});
  const el = procs[0].windows[0].entireContents()[Number(index)];
  if (action === 'press') { el.actions['AXPress'].perform(); }
  else if (action === 'set') { el.focused = true; el.value = text; }
  else if (action === 'focus') { el.focused = true; }
  return 'ok';
}
"""

_KEYS = {"enter": 36, "tab": 48, "escape": 53, "backspace": 51, "arrowdown": 125, "arrowup": 126, "space": 49}


def parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """[x, y, width, height] from an engine or script, in desktop points (None when absent or malformed)."""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        x, y, width, height = (round(float(v)) for v in value)
    except (TypeError, ValueError):
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


class MacAccessibility:
    """macOS Accessibility (the AccessibilityProvider on macOS)."""

    name = "accessibility"

    def __init__(self, application: str | None = None, *, timeout: float = 20.0) -> None:
        self.application = resolve_app(application) if application else None
        self.timeout = timeout
        self._last: dict[str, int] = {}

    def _osascript(self, script: str, *args: str, cancel: CancellationToken | None = None) -> str:
        if sys.platform != "darwin":
            raise IntegrationError("macOS Accessibility is only available on macOS.")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            returncode, out, err = run_cancellable(
                ["osascript", "-l", "JavaScript", path, *args],
                timeout=self.timeout,
                cancel=cancel,
                what="macOS Accessibility",
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if returncode != 0:
            if "assistive access" in err or "-25211" in err:
                raise AccessibilityPermissionError()
            raise IntegrationError(f"macOS Accessibility failed: {err.strip()[:300]}")
        return out.strip()

    def capability(self) -> Capability:
        if sys.platform != "darwin":
            return Capability(
                self.name,
                False,
                "macOS Accessibility exists only on macOS; here the built-in engine's platform backend "
                "provides the accessibility tree (see `highhx computer status`)",
            )
        try:
            self.observe()
        except AccessibilityPermissionError as exc:
            return Capability(self.name, False, exc.hint or exc.message)
        except IntegrationError as exc:
            return Capability(self.name, False, exc.message)
        return Capability(self.name, True, "macOS Accessibility (System Events)")

    def activate(self, application: str, *, cancel: CancellationToken | None = None) -> None:
        self.application = resolve_app(application)
        self._osascript(
            f"function run() {{ Application({json.dumps(self.application)}).activate(); return 'ok'; }}", cancel=cancel
        )

    def observe(self, *, cancel: CancellationToken | None = None) -> Observation:
        data = json.loads(self._osascript(_AX_OBSERVE, self.application or "", cancel=cancel) or "{}")
        if data.get("error"):
            error = str(data["error"])
            if any(marker in error for marker in ("assistive access", "-25211")):
                raise AccessibilityPermissionError()
            raise IntegrationError(f"{data.get('app') or self.application or 'The frontmost application'}: {error}")
        elements = []
        self._last = {}
        for n, item in enumerate(data.get("elements") or [], start=1):
            element_id = f"a{n}"
            self._last[element_id] = int(item["index"])
            elements.append(
                UIElement(
                    id=element_id,
                    role=str(item["role"]),
                    name=str(item.get("name") or ""),
                    value=str(item.get("value") or ""),
                    enabled=bool(item.get("enabled", True)),
                    focused=bool(item.get("focused")),
                    attributes={"type": "password"} if item.get("secure") else {},
                    bounds=parse_bounds(item.get("bounds")),
                    source="ax",
                )
            )
        return Observation(
            provider=self.name,
            application=str(data.get("app") or ""),
            title=str(data.get("title") or ""),
            elements=elements,
            captured_at=time.time(),
        )

    def _index(self, element_id: str) -> str:
        if element_id not in self._last:
            raise IntegrationError(f"Element {element_id} is not in the latest observation.")
        return str(self._last[element_id])

    def click(self, element_id: str, *, cancel: CancellationToken | None = None) -> None:
        self._osascript(_AX_ACT, self.application or "", self._index(element_id), "press", "", cancel=cancel)

    def type_text(self, element_id: str, text: str, *, cancel: CancellationToken | None = None) -> None:
        self._osascript(_AX_ACT, self.application or "", self._index(element_id), "set", text, cancel=cancel)

    def press(self, key: str, *, cancel: CancellationToken | None = None) -> None:
        if key not in _KEYS:
            raise IntegrationError(f"Unsupported key {key!r}")
        self._osascript(
            f"function run() {{ Application('System Events').keyCode({_KEYS[key]}); return 'ok'; }}", cancel=cancel
        )

    def scroll(self, direction: str, *, cancel: CancellationToken | None = None) -> None:
        self.press("arrowdown" if direction == "down" else "arrowup", cancel=cancel)

    def select(self, element_id: str, option: str, *, cancel: CancellationToken | None = None) -> None:
        self.type_text(element_id, option, cancel=cancel)


class AccessibilityPermissionError(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            "macOS has not granted HighhX's terminal Accessibility access.",
            hint="Allow your terminal in System Settings → Privacy & Security → Accessibility, then retry.",
        )


class TesseractOCR:
    """Local OCR of the screen (macOS ``screencapture`` / Linux ``import`` + ``tesseract``)."""

    name = "ocr"

    def capability(self) -> Capability:
        if not shutil.which("tesseract"):
            return Capability(self.name, False, "tesseract is not installed")
        if not self._capture_command(Path("x.png")):
            return Capability(self.name, False, "no screenshot tool found")
        return Capability(self.name, True, "tesseract")

    @staticmethod
    def _capture_command(path: Path) -> list[str] | None:
        if sys.platform == "darwin":
            return ["screencapture", "-x", str(path)]
        if shutil.which("import"):
            return ["import", "-window", "root", str(path)]
        return None

    def read_screen(self, *, cancel: CancellationToken | None = None) -> Observation:
        """Screenshot → tesseract → text lines. Read-only: OCR text cannot be acted on.

        Raises (never returns an empty "success") when OCR is unavailable or fails. The
        screenshot only lives in a private temporary directory for the duration of the call.
        """
        capability = self.capability()
        if not capability.available:
            raise ToolNotFoundError("tesseract", purpose="read text from the screen", hint=capability.detail)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "screen.png"
            capture = self._capture_command(image)
            if capture is None:
                raise ToolNotFoundError("screencapture", purpose="take a screenshot")
            code, _out, err = run_cancellable(capture, timeout=30, cancel=cancel, what="Screen capture")
            if code != 0 or not image.exists():
                raise IntegrationError(
                    f"Could not capture the screen: {err.strip()[:200] or f'exit code {code}'}",
                    hint="On macOS, allow your terminal in System Settings → Privacy & Security → Screen Recording.",
                )
            return self.read_image(image, cancel=cancel)

    def read_image(self, image: Path, *, cancel: CancellationToken | None = None) -> Observation:
        """Text lines of an existing image, with their bounds in the image's pixels."""
        if not shutil.which("tesseract"):
            raise ToolNotFoundError("tesseract", purpose="read text from an image")
        code, tsv, err = run_cancellable(["tesseract", str(image), "-", "tsv"], timeout=120, cancel=cancel, what="OCR")
        if code != 0:
            raise IntegrationError(f"tesseract failed: {err.strip()[:200] or f'exit code {code}'}")
        return parse_tesseract_tsv(tsv)


def parse_tesseract_tsv(tsv: str) -> Observation:
    """Group OCR words into lines of text elements (role ``text``)."""
    lines: dict[tuple[str, str, str], list[tuple[str, int, int, int, int]]] = {}
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
            left, top, width, height = (int(cols[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        if conf < 50:
            continue
        lines.setdefault((cols[2], cols[3], cols[4]), []).append((cols[11], left, top, width, height))
    elements = []
    for n, words in enumerate(lines.values(), start=1):
        text = " ".join(w[0] for w in words)
        left = min(w[1] for w in words)
        top = min(w[2] for w in words)
        right = max(w[1] + w[3] for w in words)
        bottom = max(w[2] + w[4] for w in words)
        elements.append(UIElement(f"o{n}", "text", text, bounds=(left, top, right - left, bottom - top), source="ocr"))
    return Observation(
        provider="ocr", application="screen", elements=elements, text="\n".join(e.name for e in elements)
    )
