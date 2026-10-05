"""The bridge protocol on Windows.

    user32 (ctypes)        pointer and keyboard (SendInput), windows (EnumWindows, GetWindowRect,
                           SetWindowPos), the foreground window, background delivery (PostMessage)
    UI Automation          the accessibility tree, element presses, the element at a point, menus
    (PowerShell)           — System.Windows.Automation, in one fixed HighhX script run with ``-File``,
                           so every argument is a literal string, never code
    capture, clipboard     System.Drawing / Get-Clipboard / Set-Clipboard in the same script

Coordinates are physical pixels: HighhX declares its process per-monitor DPI-aware (it has no
windows of its own, so nothing else changes). The protocol's ``command`` modifier is Ctrl here (``cmd+t``
means "new tab" on every platform). Background delivery (``app``) posts window messages, which
many applications (and every elevated one) ignore — it is reported as best effort.
"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import Structure, Union, c_int, c_long, c_size_t, c_uint, c_ulong, c_ushort, c_void_p
from pathlib import Path
from typing import Any

from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.platforms import BOUNDS_TOLERANCE, Backend, feature, png_size

VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "delete": 0x08,
    "backspace": 0x08,
    "forwarddelete": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "arrowleft": 0x25,
    "arrowup": 0x26,
    "arrowright": 0x27,
    "arrowdown": 0x28,
    "pageup": 0x21,
    "pagedown": 0x22,
    "home": 0x24,
    "end": 0x23,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}
"""The protocol's named keys as virtual-key codes (``delete`` is backspace, as in the protocol)."""
VK_MODIFIERS = {"command": 0x11, "control": 0x11, "option": 0x12, "shift": 0x10}
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYUP, UNICODE = 0x0002, 0x0004
MOVE, ABSOLUTE, VIRTUALDESK, WHEEL_V, WHEEL_H = 0x0001, 0x8000, 0x4000, 0x0800, 0x1000
MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_CLOSE = 0x0100, 0x0101, 0x0102, 0x0010
WM_BUTTONS = {"left": (0x0201, 0x0202), "right": (0x0204, 0x0205), "middle": (0x0207, 0x0208)}
WHEEL_DELTA = 120
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
SW_RESTORE = 9
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
DPI_AWARE_PER_MONITOR_V2 = -4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14


class MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", c_long),
        ("dy", c_long),
        ("mouseData", c_ulong),
        ("dwFlags", c_ulong),
        ("time", c_ulong),
        ("dwExtraInfo", c_size_t),
    ]


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", c_ushort),
        ("wScan", c_ushort),
        ("dwFlags", c_ulong),
        ("time", c_ulong),
        ("dwExtraInfo", c_size_t),
    ]


class _INPUTUNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(Structure):
    _fields_ = [("type", c_ulong), ("u", _INPUTUNION)]


class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


class POINT(Structure):
    _fields_ = [("x", c_long), ("y", c_long)]


def key_inputs(vk: int, modifiers: list[str]) -> list[INPUT]:
    """Modifiers down, the key down and up, modifiers up (in reverse)."""
    held = [VK_MODIFIERS[m] for m in dict.fromkeys(modifiers)]
    sequence = [(code, 0) for code in held] + [(vk, 0), (vk, KEYUP)] + [(code, KEYUP) for code in reversed(held)]
    out = []
    for code, flags in sequence:
        item = INPUT(type=INPUT_KEYBOARD)
        item.u.ki = KEYBDINPUT(wVk=code, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        out.append(item)
    return out


def text_inputs(text: str) -> list[INPUT]:
    """Each UTF-16 unit as a Unicode key press (layout-independent)."""
    units = text.encode("utf-16-le")
    out = []
    for i in range(0, len(units), 2):
        unit = int.from_bytes(units[i : i + 2], "little")
        for flags in (UNICODE, UNICODE | KEYUP):
            item = INPUT(type=INPUT_KEYBOARD)
            item.u.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=0)
            out.append(item)
    return out


def mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    item = INPUT(type=INPUT_MOUSE)
    item.u.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


class Win32:
    """The user32 / kernel32 / dwmapi calls the backend uses (only constructed on Windows)."""

    def __init__(self) -> None:
        dll = getattr(ctypes, "WinDLL")  # noqa: B009 - WinDLL exists only on Windows
        self.user32 = dll("user32", use_last_error=True)
        self.kernel32 = dll("kernel32", use_last_error=True)
        self.dwmapi = dll("dwmapi")
        self.user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
        self.user32.SetProcessDpiAwarenessContext(c_void_p(DPI_AWARE_PER_MONITOR_V2))  # fails harmlessly if already set
        self.user32.SendInput.argtypes = [c_uint, ctypes.POINTER(INPUT), c_int]
        self.user32.PostMessageW.argtypes = [c_void_p, c_uint, c_size_t, c_size_t]
        self.user32.SetWindowPos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
        self.user32.GetForegroundWindow.restype = c_void_p

    def send(self, inputs: list[INPUT]) -> None:
        array = (INPUT * len(inputs))(*inputs)
        if self.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) != len(inputs):
            raise EngineError(
                "refused",
                "Windows blocked the input (a higher-privileged window is in front, or the desktop is locked).",
            )

    def metrics(self, index: int) -> int:
        return int(self.user32.GetSystemMetrics(index))

    def cursor(self) -> tuple[int, int]:
        point = POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(c_void_p(hwnd))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(c_void_p(hwnd), buffer, length + 1)
        return buffer.value

    def pid(self, hwnd: int) -> int:
        pid = c_ulong()
        self.user32.GetWindowThreadProcessId(c_void_p(hwnd), ctypes.byref(pid))
        return pid.value

    def process_name(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return Path(buffer.value).stem
        finally:
            self.kernel32.CloseHandle(handle)

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = RECT()
        self.user32.GetWindowRect(c_void_p(hwnd), ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def windows(self) -> list[int]:
        """Visible, uncloaked, titled top-level windows, front to back."""
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, c_void_p, c_void_p)  # type: ignore[attr-defined]

        def visit(hwnd: int, _param: int) -> bool:
            cloaked = c_int(0)
            self.dwmapi.DwmGetWindowAttribute(
                c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            if self.user32.IsWindowVisible(c_void_p(hwnd)) and not cloaked.value and self.title(hwnd):
                found.append(int(hwnd))
            return True

        self.user32.EnumWindows(callback_type(visit), None)
        return found

    def activate(self, hwnd: int) -> None:
        if self.user32.IsIconic(c_void_p(hwnd)):
            self.user32.ShowWindow(c_void_p(hwnd), SW_RESTORE)
        self.user32.SetForegroundWindow(c_void_p(hwnd))

    def move_window(self, hwnd: int, x: int, y: int, width: int, height: int) -> bool:
        return bool(self.user32.SetWindowPos(c_void_p(hwnd), None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE))

    def post(self, hwnd: int, message: int, wparam: int, lparam_value: int) -> bool:
        return bool(self.user32.PostMessageW(c_void_p(hwnd), message, wparam, lparam_value))

    def client_point(self, hwnd: int, x: int, y: int) -> tuple[int, int]:
        point = POINT(x, y)
        self.user32.ScreenToClient(c_void_p(hwnd), ctypes.byref(point))
        return point.x, point.y

    def vk_for(self, char: str) -> tuple[int, bool]:
        """The virtual key for a character in the active layout, and whether it needs Shift."""
        value = self.user32.VkKeyScanW(ord(char))
        if value == -1:
            raise EngineError("unsupported", f"{char!r} is not on the active keyboard layout.")
        return value & 0xFF, bool(value & 0x100)


UIA_SCRIPT = r"""
param([string]$Mode, [string]$A = '', [string]$B = '', [string]$C = '', [string]$D = '', [string]$E = '', [string]$F = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing, System.Windows.Forms
Add-Type -Namespace HighhX -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@
[void][HighhX.Win32]::SetProcessDPIAware()
$UIA = [System.Windows.Automation.AutomationElement]
$Scope = [System.Windows.Automation.TreeScope]::Descendants
$Any = [System.Windows.Automation.Condition]::TrueCondition
$Roles = @{ Button='button'; SplitButton='button'; Hyperlink='link'; Edit='textbox'; Document='textbox';
  CheckBox='checkbox'; RadioButton='radio'; ComboBox='combobox'; ListItem='option'; List='listbox';
  MenuItem='menuitem'; Menu='menu'; MenuBar='menu'; TabItem='tab'; Slider='slider'; Text='text';
  Image='image'; Window='window'; Header='heading' }
function Emit($o) { $o | ConvertTo-Json -Depth 6 -Compress }
function RoleOf($el) {
  $n = $el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
  if ($Roles.ContainsKey($n)) { $Roles[$n] } else { $n.ToLower() }
}
function RootOf($proc) {
  if ($proc) {
    $p = Get-Process -Name $proc -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if (-not $p) { Emit @{ error = 'not_running' }; exit 0 }
    return $UIA::FromHandle($p.MainWindowHandle)
  }
  return $UIA::FromHandle([HighhX.Win32]::GetForegroundWindow())
}
function Describe($el) {
  $c = $el.Current
  $value = ''
  if (-not $c.IsPassword) {
    try { $value = [string]$el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch {}
  }
  if ($value.Length -gt 300) { $value = $value.Substring(0, 300) }
  $r = $c.BoundingRectangle
  $bounds = $null
  if (-not $r.IsEmpty) { $bounds = @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) }
  [ordered]@{ role = (RoleOf $el); name = [string]$c.Name; value = $value; enabled = $c.IsEnabled;
    focused = $c.HasKeyboardFocus; secure = $c.IsPassword; bounds = $bounds; pid = $c.ProcessId }
}
function Press($el) {
  foreach ($pattern in @([System.Windows.Automation.InvokePattern], [System.Windows.Automation.TogglePattern],
                         [System.Windows.Automation.SelectionItemPattern], [System.Windows.Automation.ExpandCollapsePattern])) {
    $found = $null
    if ($el.TryGetCurrentPattern($pattern::Pattern, [ref]$found)) {
      switch ($pattern.Name) { 'InvokePattern' { $found.Invoke() } 'TogglePattern' { $found.Toggle() }
        'SelectionItemPattern' { $found.Select() } 'ExpandCollapsePattern' { $found.Expand() } }
      return $true
    }
  }
  return $false
}
switch ($Mode) {
  'tree' {
    $root = RootOf $A
    $out = New-Object System.Collections.ArrayList
    foreach ($el in $root.FindAll($Scope, $Any)) {
      if ($out.Count -ge [int]$B) { break }
      try { [void]$out.Add((Describe $el)) } catch {}
    }
    $name = (Get-Process -Id $root.Current.ProcessId).ProcessName
    Emit @{ app = $name; title = [string]$root.Current.Name; elements = $out }
  }
  'click' {
    # $B name, $C role, $D index among exact matches ('' = none), $E observed bounds 'x,y,w,h' ('' = none)
    $root = RootOf $A
    $all = @($root.FindAll($Scope, $Any) | Where-Object { $C -eq '' -or $C -eq 'any' -or (RoleOf $_) -eq $C })
    $exact = @($all | Where-Object { $_.Current.Name -eq $B })
    if ($D -ne '') {
      if ([int]$D -ge $exact.Count) { Emit @{ error = 'stale'; count = $exact.Count }; exit 0 }
      $el = $exact[[int]$D]
    } elseif ($exact.Count -gt 1) {
      Emit @{ error = 'ambiguous'; names = @($exact | Select-Object -First 5 | ForEach-Object { $_.Current.Name }) }; exit 0
    } elseif ($exact.Count -eq 1) {
      $el = $exact[0]
    } else {
      $match = @($all | Where-Object { $_.Current.Name.IndexOf($B, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
      if ($match.Count -eq 0) { Emit @{ error = 'not_found' }; exit 0 }
      if ($match.Count -gt 1) {
        Emit @{ error = 'ambiguous'; names = @($match | Select-Object -First 5 | ForEach-Object { $_.Current.Name }) }; exit 0
      }
      $el = $match[0]
    }
    $described = Describe $el
    if ($E -ne '' -and $described.bounds) {
      $want = @($E.Split(',') | ForEach-Object { [int]$_ })
      for ($i = 0; $i -lt 4; $i++) {
        if ([Math]::Abs($described.bounds[$i] - $want[$i]) -gt @@TOLERANCE@@) { Emit @{ error = 'moved'; element = $described }; exit 0 }
      }
    }
    $pressed = Press $el
    Emit @{ element = $described; pressed = $pressed }
  }
  'at' {
    $el = $UIA::FromPoint((New-Object System.Windows.Point([double]$A, [double]$B)))
    $d = Describe $el
    $d['app'] = (Get-Process -Id $el.Current.ProcessId).ProcessName
    Emit $d
  }
  'menu' {
    $root = RootOf $A
    $path = $B | ConvertFrom-Json
    for ($level = 0; $level -lt $path.Count; $level++) {
      $items = @($root.FindAll($Scope, $Any) | Where-Object { (RoleOf $_) -eq 'menuitem' })
      $item = $items | Where-Object { $_.Current.Name -eq $path[$level] } | Select-Object -First 1
      if (-not $item) { Emit @{ error = 'not_found'; level = $level; names = @($items | ForEach-Object { $_.Current.Name } | Select-Object -Unique -First 15) }; exit 0 }
      if (-not $item.Current.IsEnabled) { Emit @{ error = 'disabled' }; exit 0 }
      [void](Press $item)
      Start-Sleep -Milliseconds 200
    }
    Emit @{ ok = $true }
  }
  'capture' {
    if ($B -eq '') { $area = [System.Windows.Forms.SystemInformation]::VirtualScreen }
    else { $area = New-Object System.Drawing.Rectangle([int]$B, [int]$C, [int]$D, [int]$E) }
    $bitmap = New-Object System.Drawing.Bitmap($area.Width, $area.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($area.Location, [System.Drawing.Point]::Empty, $area.Size)
    $out = $bitmap
    $limit = 0; [void][int]::TryParse($F, [ref]$limit)
    $long = [Math]::Max($area.Width, $area.Height)
    if ($limit -gt 0 -and $long -gt $limit) {
      $out = New-Object System.Drawing.Bitmap([int]($area.Width * $limit / $long), [int]($area.Height * $limit / $long))
      $scaled = [System.Drawing.Graphics]::FromImage($out)
      $scaled.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
      $scaled.DrawImage($bitmap, 0, 0, $out.Width, $out.Height)
      $scaled.Dispose()
    }
    $out.Save($A, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $bitmap.Dispose(); if ($out -ne $bitmap) { $out.Dispose() }
    Emit @{ ok = $true; x = $area.X; y = $area.Y; width = $area.Width; height = $area.Height }
  }
  'clipboard-read' { $t = Get-Clipboard -Raw -Format Text; Emit @{ text = $t } }
  'clipboard-write' { Set-Clipboard -Value $A; Emit @{ ok = $true } }
  default { Emit @{ error = 'unknown_mode' } }
}
""".replace("@@TOLERANCE@@", str(BOUNDS_TOLERANCE))


def uia_script() -> Path:
    """HighhX's fixed UI Automation script, written to its data folder (rewritten when it differs)."""
    from highhx.utils.paths import user_data_dir

    path = user_data_dir() / "engine" / "highhx-uia.ps1"
    if not path.is_file() or path.read_text(encoding="utf-8") != UIA_SCRIPT:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UIA_SCRIPT, encoding="utf-8")
    return path


class WindowsBackend(Backend):
    platform = "windows"

    def __init__(self, runner: Any, *, cancel: Any = None, api: Any = None) -> None:
        super().__init__(runner, cancel=cancel)
        self._api = api

    @property
    def api(self) -> Any:
        if self._api is None:
            if not sys.platform.startswith("win"):
                raise EngineError("unsupported_platform", "The Windows backend runs only on Windows.")
            self._api = Win32()
        return self._api

    def uia(self, mode: str, *args: str, what: str) -> dict[str, Any]:
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(uia_script())]
        raw = self.run([*argv, mode, *args], what).strip()
        try:
            data = json.loads(raw.splitlines()[-1] if raw else "{}")
        except ValueError:
            raise EngineError("failed", f"{what}: unexpected answer {raw[-160:]!r}") from None
        if not isinstance(data, dict):
            return {}
        if data.get("error") == "not_running":
            raise EngineError("not_found", f"{what}: the application is not running.")
        return data

    # ------------------------------------------------------------ capabilities
    def features(self) -> dict[str, dict[str, Any]]:
        from shutil import which

        uia = which("powershell") is not None
        ui_detail = "UI Automation (PowerShell)" if uia else "Windows PowerShell is not available"
        return {
            "accessibility": feature(uia, ui_detail),
            "screenshot": feature(uia, "System.Drawing screen copy" if uia else ui_detail),
            "window_screenshot": feature(uia, "the window's area of the screen (it must be visible)"),
            "pointer": feature(True, "SendInput"),
            "keyboard": feature(True, "SendInput (Unicode)"),
            "background_input": feature(True, "PostMessage — best effort: many applications ignore it"),
            "windows": feature(True, "EnumWindows"),
            "window_frame": feature(True, "SetWindowPos"),
            "applications": feature(True, "windows grouped by process"),
            "menus": feature(uia, ui_detail),
            "clipboard": feature(uia, "Get-Clipboard / Set-Clipboard" if uia else ui_detail),
            "element_at": feature(uia, ui_detail),
        }

    # ------------------------------------------------------------- windows
    def _window(self, hwnd: int) -> dict[str, Any]:
        api = self.api
        x, y, width, height = api.rect(hwnd)
        pid = api.pid(hwnd)
        return {
            "id": hwnd,
            "pid": pid,
            "app": api.process_name(pid),
            "title": api.title(hwnd),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "on_screen": True,
        }

    def _windows_of(self, app: str) -> list[dict[str, Any]]:
        wanted = app.lower().removesuffix(".exe")
        return [w for w in (self._window(h) for h in self.api.windows()) if w["app"].lower() == wanted]

    def _hwnd(self, app: str | None) -> int:
        if not app:
            return int(self.api.foreground())
        found = self._windows_of(app)
        if not found:
            raise EngineError("not_found", f"{app} has no visible window.", hint=f"open {app} first")
        return int(found[0]["id"])

    def op_frontmost(self) -> dict[str, Any]:
        hwnd = self.api.foreground()
        if not hwnd:
            return {"app": "", "title": ""}
        window = self._window(hwnd)
        return {"app": window["app"], "title": window["title"], "pid": window["pid"], "window": window}

    def op_running(self, app: str) -> dict[str, Any]:
        name = app if app.lower().endswith(".exe") else f"{app}.exe"
        out = self.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {name}"], f"Check {app}")
        return {"app": app, "running": name.lower() in out.lower()}

    def op_launch(self, app: str) -> dict[str, Any]:
        self.run(["cmd", "/c", "start", "", app], f"Launch {app}")
        return {"app": app, "running": self.poll(lambda: self.op_running(app)["running"], 10)}

    def op_focus(self, app: str) -> dict[str, Any]:
        hwnd = self._hwnd(app)
        self.api.activate(hwnd)
        front = ""

        def frontmost() -> bool:
            nonlocal front
            front = self.op_frontmost()["app"]
            return front.lower() == app.lower().removesuffix(".exe")

        return {"app": app, "frontmost": self.poll(frontmost, 3), "actual": front}

    def op_open_url(self, url: str, app: str | None = None) -> dict[str, Any]:
        self.run(["cmd", "/c", "start", "", *([app] if app else []), url], f"Open {url}")
        return {"url": url, "app": app or "default browser"}

    def op_quit(self, app: str) -> dict[str, Any]:
        windows = self._windows_of(app)
        if not windows:
            raise EngineError("not_found", f"{app} has no window to close.")
        for window in windows:
            self.api.post(window["id"], WM_CLOSE, 0, 0)
        gone = self.poll(lambda: not self.op_running(app)["running"], 5)
        return {
            "app": app,
            "quit": gone,
            "detail": "" if gone else f"{app} is still running (it may be asking to save)",
        }

    def op_apps(self) -> dict[str, Any]:
        front = self.api.pid(self.api.foreground()) if self.api.foreground() else 0
        seen: dict[int, dict[str, Any]] = {}
        for hwnd in self.api.windows():
            pid = self.api.pid(hwnd)
            if pid not in seen:
                seen[pid] = {
                    "name": self.api.process_name(pid),
                    "pid": pid,
                    "bundle_id": "",
                    "frontmost": pid == front,
                    "visible": True,
                }
        return {"apps": list(seen.values())}

    def op_windows(self, app: str | None = None) -> dict[str, Any]:
        found = [self._window(h) for h in self.api.windows()]
        if app:
            found = [w for w in found if w["app"].lower() == app.lower().removesuffix(".exe")]
        return {"windows": found}

    def op_window_frame(self, window: int, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        if window not in self.api.windows():
            raise EngineError("not_found", f"There is no visible window {window}.")
        if not self.api.move_window(window, x, y, width, height):
            raise EngineError("refused", f"Window {window} refused to move (it may belong to an elevated process).")
        after = self._window(window)
        return {
            "window": window,
            "app": after["app"],
            "frame": [after["x"], after["y"], after["width"], after["height"]],
        }

    # ---------------------------------------------------------------- input
    def _vk(self, key: str) -> tuple[int, list[str]]:
        if key in VK:
            return VK[key], []
        code, shift = self.api.vk_for(key)
        return code, ["shift"] if shift else []

    def op_type(self, text: str, app: str | None = None) -> dict[str, Any]:
        if app:
            hwnd = self._hwnd(app)
            for char in text:
                self.api.post(hwnd, WM_CHAR, ord(char), 0)
            return {"characters": len(text), "app": app, "background": True}
        self.api.send(text_inputs(text))
        return {"characters": len(text)}

    def _press(self, key: str, modifiers: list[str], app: str | None) -> None:
        code, extra = self._vk(key)
        if app:
            if modifiers:
                raise EngineError(
                    "unsupported", "Key combinations cannot be delivered to a background window on Windows."
                )
            hwnd = self._hwnd(app)
            self.api.post(hwnd, WM_KEYDOWN, code, 0)
            self.api.post(hwnd, WM_KEYUP, code, 0xC0000001)
            return
        self.api.send(key_inputs(code, [*modifiers, *extra]))

    def op_key(self, key: str, app: str | None = None) -> dict[str, Any]:
        self._press(key, [], app)
        return {"key": key, **({"app": app, "background": True} if app else {})}

    def op_hotkey(self, modifiers: list[str], key: str, app: str | None = None) -> dict[str, Any]:
        self._press(key, modifiers, app)
        return {"keys": "+".join([*modifiers, key]), **({"app": app, "background": True} if app else {})}

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        """A desktop pixel as SendInput's 0-65535 virtual-desktop coordinates."""
        api = self.api
        left, top = api.metrics(SM_XVIRTUALSCREEN), api.metrics(SM_YVIRTUALSCREEN)
        width, height = max(api.metrics(SM_CXVIRTUALSCREEN), 1), max(api.metrics(SM_CYVIRTUALSCREEN), 1)
        return (x - left) * 65535 // max(width - 1, 1), (y - top) * 65535 // max(height - 1, 1)

    def _move(self, x: int, y: int) -> INPUT:
        return mouse_input(MOVE | ABSOLUTE | VIRTUALDESK, *self._absolute(x, y))

    def op_move(self, x: int, y: int) -> dict[str, Any]:
        self.api.send([self._move(x, y)])
        return {"x": x, "y": y}

    def op_mouse_button(self, action: str, x: int, y: int, button: str = "left") -> dict[str, Any]:
        down, up = MOUSE_FLAGS[button]
        self.api.send([self._move(x, y), mouse_input(down if action == "down" else up)])
        return {"action": action, "x": x, "y": y, "button": button}

    def op_window_focus(self, window: int) -> dict[str, Any]:
        if window not in self.api.windows():
            raise EngineError("not_found", f"There is no visible window {window}.")
        self.api.activate(window)
        return {
            "window": window,
            "app": self.api.process_name(self.api.pid(window)),
            "frontmost": self.poll(lambda: self.api.foreground() == window, 3),
        }

    def op_cursor(self) -> dict[str, Any]:
        x, y = self.api.cursor()
        return {"x": x, "y": y}

    def op_click_at(
        self, x: int, y: int, button: str = "left", count: int = 1, app: str | None = None
    ) -> dict[str, Any]:
        down, up = MOUSE_FLAGS[button]
        if app:
            hwnd = self._hwnd(app)
            cx, cy = self.api.client_point(hwnd, x, y)
            wm_down, wm_up = WM_BUTTONS[button]
            for _ in range(count):
                self.api.post(hwnd, wm_down, 1, lparam(cx, cy))
                self.api.post(hwnd, wm_up, 0, lparam(cx, cy))
            return {"x": x, "y": y, "button": button, "count": count, "app": app, "background": True}
        self.api.send([self._move(x, y), *[mouse_input(f) for _ in range(count) for f in (down, up)]])
        return {"x": x, "y": y, "button": button, "count": count}

    def op_drag(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left", duration_ms: int = 300
    ) -> dict[str, Any]:
        down, up = MOUSE_FLAGS[button]
        self.api.send([self._move(from_x, from_y), mouse_input(down)])
        steps = 10
        for step in range(1, steps + 1):
            self.api.send(
                [self._move(from_x + (to_x - from_x) * step // steps, from_y + (to_y - from_y) * step // steps)]
            )
            self.sleep(duration_ms / 1000 / steps)
        self.api.send([mouse_input(up)])
        return {"from": [from_x, from_y], "to": [to_x, to_y], "button": button}

    def op_scroll(self, direction: str, amount: int = 1, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        inputs = [self._move(x, y)] if x is not None and y is not None else []
        notches = amount * 3
        flags = WHEEL_V if direction in ("up", "down") else WHEEL_H
        sign = 1 if direction in ("up", "right") else -1
        inputs.append(mouse_input(flags, data=sign * notches * WHEEL_DELTA))
        self.api.send(inputs)
        return {"direction": direction, "amount": amount, **({"x": x, "y": y} if x is not None else {})}

    # ---------------------------------------------------------- observation
    def op_screen(self) -> dict[str, Any]:
        api = self.api
        return {
            "width": api.metrics(SM_CXVIRTUALSCREEN),
            "height": api.metrics(SM_CYVIRTUALSCREEN),
            "x": api.metrics(SM_XVIRTUALSCREEN),
            "y": api.metrics(SM_YVIRTUALSCREEN),
            "scale": 1.0,
        }

    def op_screenshot(
        self, path: str, window: int | None = None, region: list[int] | None = None, max_size: int | None = None
    ) -> dict[str, Any]:
        area: list[str] = [str(v) for v in region] if region else ["", "", "", ""]
        if window is not None:
            if window not in self.api.windows():
                raise EngineError("not_found", f"There is no visible window {window}.")
            area = [str(v) for v in self.api.rect(window)]
        data = self.uia("capture", path, *area, str(max_size or ""), what="Capture the screen")
        if not Path(path).is_file():
            raise EngineError("failed", "The screen capture produced no file.")
        width, height = png_size(Path(path))
        wide = int(data.get("width") or width)
        return {
            "path": path,
            "width": width,
            "height": height,
            "scale": width / wide if wide else 1.0,
            "origin": [int(data.get("x") or 0), int(data.get("y") or 0)],
            "window": window,
            "region": region,
        }

    def op_clipboard_read(self) -> dict[str, Any]:
        text = self.uia("clipboard-read", what="Read the clipboard").get("text")
        return {"text": text or "", "has_text": text is not None}

    def op_clipboard_write(self, text: str) -> dict[str, Any]:
        self.uia("clipboard-write", text, what="Write the clipboard")
        return {"characters": len(text)}

    def op_inspect(self, app: str | None = None, limit: int = 100) -> dict[str, Any]:
        data = self.uia("tree", app or "", str(limit), what="Read the accessibility tree")
        return {"app": data.get("app") or "", "title": data.get("title") or "", "elements": data.get("elements") or []}

    def op_click(
        self,
        name: str,
        role: str | None = None,
        app: str | None = None,
        index: int | None = None,
        bounds: list[int] | None = None,
    ) -> dict[str, Any]:
        observed = ",".join(str(v) for v in bounds) if bounds else ""
        data = self.uia(
            "click", app or "", name, role or "", "" if index is None else str(index), observed, what=f"Click {name}"
        )
        label = f"{role or 'element'} named {name!r}"
        error = data.get("error")
        if error == "not_found":
            raise EngineError("not_found", f"No {label}.")
        if error == "ambiguous":
            names = ", ".join(repr(n) for n in data.get("names") or [])
            raise EngineError("ambiguous_target", f"{name!r} matches several elements ({names}); say which one.")
        if error in ("stale", "moved"):
            raise EngineError(
                "stale_target",
                f"The {label} that was observed is gone or has moved; the UI changed.",
                hint="Observe again and choose the element from the new observation.",
            )
        element = data.get("element") or {}
        if not data.get("pressed"):
            bounds = element.get("bounds")
            if not bounds:
                raise EngineError("refused", f"{name!r} has no action and no position to click.")
            x, y, w, h = bounds
            self.op_click_at(x + w // 2, y + h // 2)
        return {"app": app or self.op_frontmost()["app"], "role": element.get("role"), "name": element.get("name")}

    def op_element_at(self, x: int, y: int) -> dict[str, Any]:
        return self.uia("at", str(x), str(y), what="Find the element at a point")

    def op_menu(self, app: str, path: list[str]) -> dict[str, Any]:
        data = self.uia("menu", app, json.dumps(path), what=f"Choose {' > '.join(path)}")
        if data.get("error") == "not_found":
            level = int(data.get("level") or 0)
            names = ", ".join(str(n) for n in data.get("names") or [])
            raise EngineError("not_found", f"{app} has no menu item {path[level]!r} there (it has: {names}).")
        if data.get("error") == "disabled":
            raise EngineError("refused", f"{' > '.join(path)} is disabled in {app}.")
        return {"app": app, "path": path}

    def op_verify(
        self, check: str, app: str | None = None, name: str | None = None, role: str | None = None
    ) -> dict[str, Any]:
        if check == "frontmost":
            front = self.op_frontmost()["app"]
            ok = bool(app) and front.lower() == str(app).lower().removesuffix(".exe")
            return {"ok": ok, "detail": f"{front} is frontmost"}
        if not app and check in ("running", "window"):
            raise EngineError("invalid_request", f"verify {check} needs app")
        if check == "running":
            ok = self.op_running(str(app))["running"]
            return {"ok": ok, "detail": f"{app} is {'running' if ok else 'not running'}"}
        if check == "window":
            titles = [w["title"] for w in self._windows_of(str(app))]
            ok = bool(titles) and (not name or any(name.lower() in t.lower() for t in titles))
            return {"ok": ok, "detail": f"{len(titles)} window(s)", "windows": titles}
        if not name:
            raise EngineError("invalid_request", "verify element needs name")
        found = [e for e in self.op_inspect(app, 300)["elements"] if name.lower() in str(e.get("name", "")).lower()]
        return {"ok": bool(found), "detail": f"{name!r} {'found' if found else 'not found'}"}
