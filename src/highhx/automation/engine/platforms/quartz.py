"""macOS native calls for the built-in engine: CoreGraphics events and windows, the Accessibility
API and CoreFoundation values — through ``ctypes`` (standard library; no PyObjC).

Only what the engine's macOS backend needs, each a direct call into the OS frameworks:

    permissions     AXIsProcessTrusted · CGPreflightScreenCaptureAccess
    display         CGMainDisplayID · CGDisplayBounds · CGDisplayCopyDisplayMode (pixel scale)
    windows         CGWindowListCopyWindowInfo (on-screen, front to back)
    pointer         CGEventCreateMouseEvent · CGEventCreateScrollWheelEvent2 · CGEventGetLocation
    keyboard        CGEventCreateKeyboardEvent · CGEventKeyboardSetUnicodeString (background delivery)
    delivery        CGEventPost (the HID stream: the frontmost app) · CGEventPostToPid (one process)
    elements        AXUIElementCopyElementAtPosition · AXUIElementCopyAttributeValue ·
                    AXUIElementSetAttributeValue (window position and size)

Nothing is loaded until first used, so importing this module is safe on every platform.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, Structure, byref, c_bool, c_char_p, c_double, c_int32, c_int64, c_long, c_uint16, c_uint32
from ctypes import c_void_p as ref
from dataclasses import dataclass
from functools import cache
from typing import Any

FRAMEWORKS = "/System/Library/Frameworks/{0}.framework/{0}"
UTF8 = 0x08000100  # kCFStringEncodingUTF8

# CGEventType
LEFT_DOWN, LEFT_UP, RIGHT_DOWN, RIGHT_UP, MOVED, LEFT_DRAGGED, RIGHT_DRAGGED = 1, 2, 3, 4, 5, 6, 7
OTHER_DOWN, OTHER_UP, OTHER_DRAGGED = 25, 26, 27
BUTTONS = {
    "left": (LEFT_DOWN, LEFT_UP, LEFT_DRAGGED, 0),
    "right": (RIGHT_DOWN, RIGHT_UP, RIGHT_DRAGGED, 1),
    "middle": (OTHER_DOWN, OTHER_UP, OTHER_DRAGGED, 2),
}
CLICK_STATE = 1  # kCGMouseEventClickState
HID_TAP = 0  # kCGHIDEventTap
SCROLL_LINES = 1  # kCGScrollEventUnitLine
FLAGS = {"shift": 0x00020000, "control": 0x00040000, "option": 0x00080000, "command": 0x00100000}
ON_SCREEN, EXCLUDE_DESKTOP = 1, 16  # kCGWindowListOptionOnScreenOnly, …ExcludeDesktopElements
AX_POINT, AX_SIZE = 1, 2  # kAXValueCGPointType, kAXValueCGSizeType


class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class CGRect(Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class NativeError(RuntimeError):
    """A native call failed (``code`` is an AXError or similar when there is one)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Frameworks:
    cg: Any
    cf: Any
    ax: Any


def _sig(lib: Any, name: str, restype: Any, *argtypes: Any) -> None:
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = restype, list(argtypes)


@cache
def frameworks() -> Frameworks:
    """The loaded frameworks, with every function's signature declared (once)."""
    cg = ctypes.CDLL(FRAMEWORKS.format("CoreGraphics"))
    cf = ctypes.CDLL(FRAMEWORKS.format("CoreFoundation"))
    ax = ctypes.CDLL(FRAMEWORKS.format("ApplicationServices"))
    _sig(cf, "CFRelease", None, ref)
    _sig(cf, "CFStringCreateWithCString", ref, ref, c_char_p, c_uint32)
    _sig(cf, "CFStringGetCString", c_bool, ref, c_char_p, c_long, c_uint32)
    _sig(cf, "CFStringGetLength", c_long, ref)
    _sig(cf, "CFGetTypeID", c_long, ref)
    _sig(cf, "CFStringGetTypeID", c_long)
    _sig(cf, "CFNumberGetTypeID", c_long)
    _sig(cf, "CFBooleanGetTypeID", c_long)
    _sig(cf, "CFNumberGetValue", c_bool, ref, c_long, ref)
    _sig(cf, "CFBooleanGetValue", c_bool, ref)
    _sig(cf, "CFArrayGetCount", c_long, ref)
    _sig(cf, "CFArrayGetValueAtIndex", ref, ref, c_long)
    _sig(cf, "CFDictionaryGetValue", ref, ref, ref)
    _sig(cg, "CGMainDisplayID", c_uint32)
    _sig(cg, "CGDisplayBounds", CGRect, c_uint32)
    _sig(cg, "CGDisplayCopyDisplayMode", ref, c_uint32)
    _sig(cg, "CGDisplayModeGetPixelWidth", ctypes.c_size_t, ref)
    _sig(cg, "CGDisplayModeRelease", None, ref)
    _sig(cg, "CGPreflightScreenCaptureAccess", c_bool)
    _sig(cg, "CGWindowListCopyWindowInfo", ref, c_uint32, c_uint32)
    _sig(cg, "CGRectMakeWithDictionaryRepresentation", c_bool, ref, POINTER(CGRect))
    _sig(cg, "CGEventCreate", ref, ref)
    _sig(cg, "CGEventGetLocation", CGPoint, ref)
    _sig(cg, "CGEventCreateMouseEvent", ref, ref, c_uint32, CGPoint, c_uint32)
    _sig(cg, "CGEventCreateScrollWheelEvent2", ref, ref, c_uint32, c_uint32, c_int32, c_int32, c_int32)
    _sig(cg, "CGEventCreateKeyboardEvent", ref, ref, c_uint16, c_bool)
    _sig(cg, "CGEventKeyboardSetUnicodeString", None, ref, c_long, POINTER(c_uint16))
    _sig(cg, "CGEventSetFlags", None, ref, ctypes.c_uint64)
    _sig(cg, "CGEventSetLocation", None, ref, CGPoint)
    _sig(cg, "CGEventSetIntegerValueField", None, ref, c_uint32, c_int64)
    _sig(cg, "CGEventPost", None, c_uint32, ref)
    _sig(cg, "CGEventPostToPid", None, c_int32, ref)
    _sig(ax, "AXIsProcessTrusted", c_bool)
    _sig(ax, "AXUIElementCreateSystemWide", ref)
    _sig(ax, "AXUIElementCreateApplication", ref, c_int32)
    _sig(ax, "AXUIElementCopyElementAtPosition", c_int32, ref, ctypes.c_float, ctypes.c_float, POINTER(ref))
    _sig(ax, "AXUIElementCopyAttributeValue", c_int32, ref, ref, POINTER(ref))
    _sig(ax, "AXUIElementSetAttributeValue", c_int32, ref, ref, ref)
    _sig(ax, "AXUIElementGetPid", c_int32, ref, POINTER(c_int32))
    _sig(ax, "AXValueCreate", ref, c_uint32, ref)
    _sig(ax, "AXValueGetValue", c_bool, ref, c_uint32, ref)
    return Frameworks(cg, cf, ax)


# ---------------------------------------------------------------- CoreFoundation
@cache
def _cfstr(text: str) -> int:
    """An immortal CFString for a constant key (created once, never released)."""
    value = frameworks().cf.CFStringCreateWithCString(None, text.encode(), UTF8)
    if not value:
        raise NativeError(f"could not create the string {text!r}")
    return int(value)


def _release(value: Any) -> None:
    if value:
        frameworks().cf.CFRelease(value)


def to_python(value: Any) -> Any:
    """A CFString, CFNumber or CFBoolean as a Python value (None for anything else)."""
    if not value:
        return None
    cf = frameworks().cf
    kind = cf.CFGetTypeID(value)
    if kind == cf.CFStringGetTypeID():
        size = cf.CFStringGetLength(value) * 4 + 1
        buffer = ctypes.create_string_buffer(size)
        return buffer.value.decode("utf-8", "replace") if cf.CFStringGetCString(value, buffer, size, UTF8) else ""
    if kind == cf.CFBooleanGetTypeID():
        return bool(cf.CFBooleanGetValue(value))
    if kind == cf.CFNumberGetTypeID():
        number = c_double()
        cf.CFNumberGetValue(value, 13, byref(number))  # kCFNumberDoubleType
        return int(number.value) if number.value.is_integer() else number.value
    return None


def _dict_get(dictionary: Any, key: str) -> Any:
    return frameworks().cf.CFDictionaryGetValue(dictionary, _cfstr(key))


# ------------------------------------------------------------------ permissions
def accessibility_trusted() -> bool:
    return bool(frameworks().ax.AXIsProcessTrusted())


def screen_capture_allowed() -> bool:
    """Screen Recording permission (without it captures show only the desktop and HighhX's own windows)."""
    return bool(frameworks().cg.CGPreflightScreenCaptureAccess())


# ----------------------------------------------------------------------- display
def screen() -> dict[str, Any]:
    fw = frameworks()
    display = fw.cg.CGMainDisplayID()
    bounds = fw.cg.CGDisplayBounds(display)
    scale = 1.0
    mode = fw.cg.CGDisplayCopyDisplayMode(display)
    if mode:
        pixels = fw.cg.CGDisplayModeGetPixelWidth(mode)
        fw.cg.CGDisplayModeRelease(mode)
        if bounds.size.width:
            scale = round(pixels / bounds.size.width, 3)
    return {
        "width": int(bounds.size.width),
        "height": int(bounds.size.height),
        "x": int(bounds.origin.x),
        "y": int(bounds.origin.y),
        "scale": scale,
    }


def windows() -> list[dict[str, Any]]:
    """On-screen application windows (layer 0), front to back. Titles are empty without Screen
    Recording permission — the OS withholds them, HighhX does not invent them."""
    fw = frameworks()
    array = fw.cg.CGWindowListCopyWindowInfo(ON_SCREEN | EXCLUDE_DESKTOP, 0)
    if not array:
        return []
    out = []
    try:
        for index in range(fw.cf.CFArrayGetCount(array)):
            info = fw.cf.CFArrayGetValueAtIndex(array, index)
            if to_python(_dict_get(info, "kCGWindowLayer")) != 0:
                continue
            rect = CGRect()
            bounds = _dict_get(info, "kCGWindowBounds")
            if not bounds or not fw.cg.CGRectMakeWithDictionaryRepresentation(bounds, byref(rect)):
                continue
            out.append(
                {
                    "id": int(to_python(_dict_get(info, "kCGWindowNumber")) or 0),
                    "pid": int(to_python(_dict_get(info, "kCGWindowOwnerPID")) or 0),
                    "app": str(to_python(_dict_get(info, "kCGWindowOwnerName")) or ""),
                    "title": str(to_python(_dict_get(info, "kCGWindowName")) or ""),
                    "x": int(rect.origin.x),
                    "y": int(rect.origin.y),
                    "width": int(rect.size.width),
                    "height": int(rect.size.height),
                    "on_screen": True,
                }
            )
    finally:
        _release(array)
    return out


# ----------------------------------------------------------------------- pointer
def cursor() -> tuple[int, int]:
    fw = frameworks()
    event = fw.cg.CGEventCreate(None)
    try:
        point = fw.cg.CGEventGetLocation(event)
    finally:
        _release(event)
    return round(point.x), round(point.y)


def _post(event: Any, pid: int | None) -> None:
    fw = frameworks()
    if not event:
        raise NativeError("the event could not be created")
    try:
        if pid:
            fw.cg.CGEventPostToPid(pid, event)
        else:
            fw.cg.CGEventPost(HID_TAP, event)
    finally:
        _release(event)


def _mouse(kind: int, x: float, y: float, button: int, *, clicks: int = 0, pid: int | None = None) -> None:
    fw = frameworks()
    event = fw.cg.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), button)
    if event and clicks:
        fw.cg.CGEventSetIntegerValueField(event, CLICK_STATE, clicks)
    _post(event, pid)


def move(x: float, y: float) -> None:
    _mouse(MOVED, x, y, 0)


def click(x: float, y: float, *, button: str = "left", count: int = 1, pid: int | None = None) -> None:
    """``count`` presses at one point; each carries its click state, so the second is a double click."""
    down, up, _dragged, number = BUTTONS[button]
    if not pid:
        move(x, y)
    for n in range(1, count + 1):
        _mouse(down, x, y, number, clicks=n, pid=pid)
        _mouse(up, x, y, number, clicks=n, pid=pid)


def drag(start: tuple[float, float], end: tuple[float, float], *, button: str = "left", duration: float = 0.3) -> None:
    down, up, dragged, number = BUTTONS[button]
    move(*start)
    _mouse(down, *start, number, clicks=1)
    steps = max(2, int(duration / 0.016))
    for step in range(1, steps + 1):
        t = step / steps
        _mouse(dragged, start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t, number)
        time.sleep(duration / steps)
    _mouse(up, *end, number, clicks=1)


def scroll(dy: int, dx: int = 0, *, at: tuple[float, float] | None = None) -> None:
    """Wheel scrolling in lines (positive dy scrolls content down, as a wheel turned toward you).
    The event carries its own location, so it reaches the view under ``at`` even if the pointer
    move before it has not been processed yet."""
    fw = frameworks()
    if at is not None:
        move(*at)
    event = fw.cg.CGEventCreateScrollWheelEvent2(None, SCROLL_LINES, 2, -dy, -dx, 0)
    if event:
        fw.cg.CGEventSetLocation(event, CGPoint(*(at if at is not None else cursor())))
    _post(event, None)


# ---------------------------------------------------------------------- keyboard
def key(code: int, modifiers: list[str], *, pid: int | None = None) -> None:
    fw = frameworks()
    flags = 0
    for modifier in modifiers:
        flags |= FLAGS[modifier]
    for down in (True, False):
        event = fw.cg.CGEventCreateKeyboardEvent(None, code, down)
        if event and flags:
            fw.cg.CGEventSetFlags(event, flags)
        _post(event, pid)


def type_text(text: str, *, pid: int | None = None) -> None:
    """Unicode text as keyboard events (chunks of 16 UTF-16 units, the events' limit)."""
    fw = frameworks()
    units = text.encode("utf-16-le")
    codes = [int.from_bytes(units[i : i + 2], "little") for i in range(0, len(units), 2)]
    for start in range(0, len(codes), 16):
        chunk = codes[start : start + 16]
        buffer = (c_uint16 * len(chunk))(*chunk)
        for down in (True, False):
            event = fw.cg.CGEventCreateKeyboardEvent(None, 0, down)
            if event:
                fw.cg.CGEventKeyboardSetUnicodeString(event, len(chunk), buffer)
            _post(event, pid)
        time.sleep(0.005)


# ---------------------------------------------------------------------- elements
AX_ROLES = {
    "AXButton": "button",
    "AXMenuButton": "button",
    "AXLink": "link",
    "AXTextField": "textbox",
    "AXTextArea": "textbox",
    "AXSecureTextField": "textbox",
    "AXSearchField": "searchbox",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "combobox",
    "AXComboBox": "combobox",
    "AXMenuItem": "menuitem",
    "AXMenuBarItem": "menuitem",
    "AXTabGroup": "tab",
    "AXSlider": "slider",
    "AXStaticText": "text",
    "AXHeading": "heading",
    "AXImage": "image",
    "AXWindow": "window",
}
"""The same role names as the engine's accessibility observation (``computer.desktop``)."""


def _attribute(element: Any, name: str) -> Any:
    value = ref()
    error = frameworks().ax.AXUIElementCopyAttributeValue(element, _cfstr(name), byref(value))
    return value if error == 0 else None


def _string_attribute(element: Any, name: str) -> str:
    value = _attribute(element, name)
    try:
        result = to_python(value)
    finally:
        _release(value)
    return "" if result is None else str(result)


def _frame(element: Any) -> tuple[int, int, int, int] | None:
    fw = frameworks()
    position, size = _attribute(element, "AXPosition"), _attribute(element, "AXSize")
    try:
        point, extent = CGPoint(), CGSize()
        if not (position and size):
            return None
        if not (
            fw.ax.AXValueGetValue(position, AX_POINT, byref(point))
            and fw.ax.AXValueGetValue(size, AX_SIZE, byref(extent))
        ):
            return None
        return int(point.x), int(point.y), int(extent.width), int(extent.height)
    finally:
        _release(position)
        _release(size)


def element_at(x: float, y: float) -> dict[str, Any]:
    """The accessibility element under a desktop point."""
    fw = frameworks()
    system = fw.ax.AXUIElementCreateSystemWide()
    element = ref()
    try:
        error = fw.ax.AXUIElementCopyElementAtPosition(system, float(x), float(y), byref(element))
    finally:
        _release(system)
    if error != 0 or not element:
        raise NativeError(f"no accessibility element at ({x}, {y}) (AXError {error})", error)
    try:
        pid = c_int32()
        fw.ax.AXUIElementGetPid(element, byref(pid))
        ax_role = _string_attribute(element, "AXRole")
        name = (
            _string_attribute(element, "AXTitle")
            or _string_attribute(element, "AXDescription")
            or _string_attribute(element, "AXHelp")
        )
        secure = ax_role == "AXSecureTextField"
        return {
            "pid": pid.value,
            "role": AX_ROLES.get(ax_role, ax_role.removeprefix("AX").lower() or "unknown"),
            "ax_role": ax_role,
            "name": name,
            "value": "" if secure else _string_attribute(element, "AXValue")[:300],
            "secure": secure,
            "bounds": list(_frame(element) or ()),
        }
    finally:
        _release(element)


def set_window_frame(pid: int, match: tuple[int, int, int, int], frame: tuple[int, int, int, int]) -> bool:
    """Move/resize the window of ``pid`` whose frame is ``match`` (the frame CoreGraphics reported)."""
    fw = frameworks()
    app = fw.ax.AXUIElementCreateApplication(pid)
    windows = _attribute(app, "AXWindows")
    try:
        if not windows:
            return False
        for index in range(fw.cf.CFArrayGetCount(windows)):
            window = fw.cf.CFArrayGetValueAtIndex(windows, index)
            if _frame(window) != match:
                continue
            point, size = CGPoint(frame[0], frame[1]), CGSize(frame[2], frame[3])
            position_value = fw.ax.AXValueCreate(AX_POINT, byref(point))
            size_value = fw.ax.AXValueCreate(AX_SIZE, byref(size))
            try:
                moved = fw.ax.AXUIElementSetAttributeValue(window, _cfstr("AXPosition"), position_value)
                resized = fw.ax.AXUIElementSetAttributeValue(window, _cfstr("AXSize"), size_value)
            finally:
                _release(position_value)
                _release(size_value)
            if moved or resized:
                raise NativeError(
                    f"the window refused to move or resize (AXError {moved or resized})", moved or resized
                )
            return True
        return False
    finally:
        _release(windows)
        _release(app)
