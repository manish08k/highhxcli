"""ComputerState: one immutable observation of a computer, fused from every perception source.

    ComputerState
      surface         desktop · browser · android
      timestamp       when it was observed (time.time())
      device          OS, model, screen size and scale
      active_app / active_window / windows / processes (when permitted)
      cursor / focused / viewport
      browser         URL, title, tabs, loading state (browser surface)
      elements        UI elements with bounds, state and *provenance* (dom · ax · ocr · vision)
      text / ocr_text visible text from the structured tree / from OCR (untrusted content)
      screenshot      a reference to the capture (path, size, scale, digest), never the pixels
      perception      what each source did: ok, skipped, failed, how long it took

A state never changes once observed: every collection is a tuple and the dataclasses are frozen.
The next observation is a new state, so the agent loop and the verifiers can compare a *before*
and an *after*. Providers keep their own mutable working model
(:class:`~highhx.computer.model.UIElement`). :func:`StateElement.from_ui` and
:meth:`StateElement.to_ui` convert between the two without losing anything.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.computer.model import Observation, UIElement

SURFACES = ("desktop", "browser", "android")
SOURCES = ("dom", "ax", "ocr", "vision", "android")
"""Where an element was perceived. ``android`` is the uiautomator hierarchy (an accessibility tree)."""

Bounds = tuple[int, int, int, int]
"""x, y, width, height in the surface's input coordinates (desktop points, CSS pixels, device pixels)."""


def _pair(value: Any) -> tuple[int, int] | None:
    if not value:
        return None
    return int(value[0]), int(value[1])


def center(bounds: Bounds) -> tuple[int, int]:
    x, y, width, height = bounds
    return round(x + width / 2), round(y + height / 2)


def overlap(a: Bounds, b: Bounds) -> float:
    """Intersection over union of two boxes (0 … 1)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def contains(bounds: Bounds, point: tuple[int, int]) -> bool:
    x, y, width, height = bounds
    return x <= point[0] < x + width and y <= point[1] < y + height


@dataclass(frozen=True)
class StateElement:
    id: str
    role: str
    name: str
    value: str = ""
    """Always empty for secret fields (passwords, card numbers, one-time codes)."""
    bounds: Bounds | None = None
    enabled: bool = True
    focused: bool = False
    checked: bool | None = None
    visible: bool = True
    sources: tuple[str, ...] = ("dom",)
    attributes: tuple[tuple[str, str], ...] = ()
    confidence: float = 1.0
    """1.0 for structured sources; OCR and vision report how sure they were."""

    def attr(self, key: str, default: str = "") -> str:
        for k, v in self.attributes:
            if k == key:
                return v
        return default

    @property
    def secret(self) -> bool:
        kind = self.attr("type").lower()
        auto = self.attr("autocomplete").lower()
        return kind == "password" or "password" in auto or "cc-" in auto or "one-time-code" in auto

    @property
    def center(self) -> tuple[int, int] | None:
        return center(self.bounds) if self.bounds else None

    def label(self) -> str:
        return f'{self.role} "{self.name or "(unnamed)"}"'

    @classmethod
    def from_ui(cls, element: UIElement, *, source: str | None = None, confidence: float = 1.0) -> StateElement:
        return cls(
            id=element.id,
            role=element.role,
            name=element.name,
            value="" if element.secret else element.value,
            bounds=tuple(element.bounds) if element.bounds else None,  # type: ignore[arg-type]
            enabled=element.enabled,
            focused=element.focused,
            checked=element.checked,
            visible=element.visible,
            sources=(source or element.source,),
            attributes=tuple(sorted((str(k), str(v)) for k, v in element.attributes.items())),
            confidence=confidence,
        )

    def to_ui(self) -> UIElement:
        from highhx.computer.model import UIElement

        return UIElement(
            id=self.id,
            role=self.role,
            name=self.name,
            value=self.value,
            enabled=self.enabled,
            focused=self.focused,
            checked=self.checked,
            visible=self.visible,
            attributes=dict(self.attributes),
            bounds=self.bounds,
            source=self.sources[0] if self.sources else "dom",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": "" if self.secret else self.value,
            "bounds": list(self.bounds) if self.bounds else None,
            "enabled": self.enabled,
            "focused": self.focused,
            "checked": self.checked,
            "visible": self.visible,
            "sources": list(self.sources),
            "attributes": dict(self.attributes),
            "confidence": round(self.confidence, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateElement:
        bounds = data.get("bounds")
        return cls(
            id=str(data.get("id", "")),
            role=str(data.get("role", "")),
            name=str(data.get("name", "")),
            value=str(data.get("value", "") or ""),
            bounds=tuple(int(v) for v in bounds) if bounds else None,  # type: ignore[arg-type]
            enabled=bool(data.get("enabled", True)),
            focused=bool(data.get("focused", False)),
            checked=data.get("checked"),
            visible=bool(data.get("visible", True)),
            sources=tuple(data.get("sources") or (data.get("source") or "dom",)),
            attributes=tuple(sorted((str(k), str(v)) for k, v in (data.get("attributes") or {}).items())),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class ScreenshotRef:
    """Where a capture is and what it was. The pixels stay in the file (or in ``data`` while the
    observation that took it is in use). They are never serialized."""

    width: int
    height: int
    scale: float = 1.0
    """Screenshot pixels per input point (2.0 on Retina)."""
    sha256: str = ""
    path: str = ""
    captured_at: float = 0.0
    data: bytes | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "sha256": self.sha256,
            "path": self.path,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_bytes(cls, data: bytes, *, scale: float = 1.0, path: str = "") -> ScreenshotRef:
        from highhx.perception.png import png_size

        width, height = png_size(data)
        return cls(width, height, scale, hashlib.sha256(data).hexdigest(), path, time.time(), data)


@dataclass(frozen=True)
class WindowInfo:
    id: str
    app: str
    title: str = ""
    bounds: Bounds | None = None
    focused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app": self.app,
            "title": self.title,
            "bounds": list(self.bounds) if self.bounds else None,
            "focused": self.focused,
        }


@dataclass(frozen=True)
class TabInfo:
    id: str
    url: str
    title: str = ""
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "active": self.active}


@dataclass(frozen=True)
class BrowserState:
    url: str = ""
    title: str = ""
    tabs: tuple[TabInfo, ...] = ()
    loading: bool = False
    dialog: str = ""
    """The text of an open JavaScript dialog, if any."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "tabs": [t.to_dict() for t in self.tabs],
            "loading": self.loading,
            "dialog": self.dialog,
        }


@dataclass(frozen=True)
class DeviceInfo:
    os: str = ""
    """darwin · win32 · linux · android"""
    model: str = ""
    serial: str = ""
    screen: tuple[int, int] | None = None
    scale: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "os": self.os,
            "model": self.model,
            "serial": self.serial,
            "screen": list(self.screen) if self.screen else None,
            "scale": self.scale,
        }


@dataclass(frozen=True)
class PerceptionRecord:
    """What one perception source did for this observation."""

    source: str
    status: str
    """ok · skipped · failed · unavailable"""
    detail: str = ""
    seconds: float = 0.0
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 4),
            "count": self.count,
        }


@dataclass(frozen=True)
class ComputerState:
    surface: str
    timestamp: float = field(default_factory=time.time)
    device: DeviceInfo = field(default_factory=DeviceInfo)
    active_app: str = ""
    active_window: WindowInfo | None = None
    windows: tuple[WindowInfo, ...] = ()
    processes: tuple[str, ...] = ()
    cursor: tuple[int, int] | None = None
    focused: str = ""
    """The id of the element with keyboard focus (where typed keys go)."""
    selected: str = ""
    """The id of the selected element (a chosen tab, row or option), when the source reports it."""
    viewport: tuple[int, int] | None = None
    browser: BrowserState | None = None
    elements: tuple[StateElement, ...] = ()
    text: str = ""
    ocr_text: str = ""
    screenshot: ScreenshotRef | None = None
    perception: tuple[PerceptionRecord, ...] = ()
    network: tuple[tuple[str, str], ...] = ()
    """Network hints: the page still loading, an offline device …"""
    cwd: str = ""
    """The filesystem context of the task (the project or sandbox workspace)."""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise ValueError(f"unknown surface {self.surface!r} (expected one of: {', '.join(SURFACES)})")

    # ---------------------------------------------------------------- lookup
    def element(self, element_id: str) -> StateElement | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def find(self, *, role: str | None = None, name: str | None = None, exact: bool = False) -> list[StateElement]:
        wanted = " ".join((name or "").lower().split())
        out = []
        for e in self.elements:
            if role and e.role != role:
                continue
            if name is not None:
                have = " ".join(e.name.lower().split())
                if (have != wanted) if exact else (wanted not in have):
                    continue
            out.append(e)
        return out

    def at(self, point: tuple[int, int]) -> StateElement | None:
        """The smallest element whose bounds contain ``point``."""
        hits = [e for e in self.elements if e.bounds and contains(e.bounds, point)]
        return min(hits, key=lambda e: e.bounds[2] * e.bounds[3]) if hits else None  # type: ignore[index]

    @property
    def url(self) -> str:
        return self.browser.url if self.browser else ""

    @property
    def title(self) -> str:
        if self.browser and self.browser.title:
            return self.browser.title
        return self.active_window.title if self.active_window else ""

    @property
    def all_text(self) -> str:
        return "\n".join(t for t in (self.text, self.ocr_text) if t)

    def meta(self, key: str, default: str = "") -> str:
        return next((v for k, v in self.metadata if k == key), default)

    # ------------------------------------------------------------- identity
    def fingerprint(self) -> str:
        """What counts as "the screen changed": location, title, text and element states. The
        screenshot digest is included only when there is no structured content to compare."""
        parts: dict[str, Any] = {
            "surface": self.surface,
            "app": self.active_app,
            "window": self.active_window.id if self.active_window else "",
            "url": self.url,
            "title": self.title,
            "text": self.text[:4000],
            "elements": [(e.role, e.name, "" if e.secret else e.value, e.checked, e.enabled) for e in self.elements],
        }
        if not self.elements and not self.text and self.screenshot is not None:
            parts["pixels"] = self.screenshot.sha256
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]

    def with_(self, **changes: Any) -> ComputerState:
        """A new state with ``changes`` (the original is untouched)."""
        return replace(self, **changes)

    # ------------------------------------------------------------ rendering
    def summary(self, limit: int = 80) -> str:
        head = self.active_app or self.surface
        if self.title:
            head += f" — {self.title}"
        if self.url:
            head += f" <{self.url}>"
        lines = [head]
        for e in self.elements[:limit]:
            where = f" @{list(e.bounds)}" if e.bounds else ""
            lines.append(f"  [{e.id}] {e.label()}{where} ({'+'.join(e.sources)})")
        if len(self.elements) > limit:
            lines.append(f"  … {len(self.elements) - limit} more elements")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "timestamp": self.timestamp,
            "fingerprint": self.fingerprint(),
            "device": self.device.to_dict(),
            "active_app": self.active_app,
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "windows": [w.to_dict() for w in self.windows],
            "processes": list(self.processes),
            "cursor": list(self.cursor) if self.cursor else None,
            "focused": self.focused,
            "selected": self.selected,
            "viewport": list(self.viewport) if self.viewport else None,
            "browser": self.browser.to_dict() if self.browser else None,
            "elements": [e.to_dict() for e in self.elements],
            "text": self.text,
            "ocr_text": self.ocr_text,
            "screenshot": self.screenshot.to_dict() if self.screenshot else None,
            "perception": [p.to_dict() for p in self.perception],
            "network": dict(self.network),
            "cwd": self.cwd,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComputerState:
        def window(w: dict[str, Any] | None) -> WindowInfo | None:
            if not w:
                return None
            b = w.get("bounds")
            return WindowInfo(
                str(w.get("id", "")),
                str(w.get("app", "")),
                str(w.get("title", "")),
                tuple(int(v) for v in b) if b else None,  # type: ignore[arg-type]
                bool(w.get("focused", False)),
            )

        browser = data.get("browser")
        device = data.get("device") or {}
        shot = data.get("screenshot")
        return cls(
            surface=str(data.get("surface", "desktop")),
            timestamp=float(data.get("timestamp") or time.time()),
            device=DeviceInfo(
                str(device.get("os", "")),
                str(device.get("model", "")),
                str(device.get("serial", "")),
                _pair(device.get("screen")),
                float(device.get("scale", 1.0)),
            ),
            active_app=str(data.get("active_app", "")),
            active_window=window(data.get("active_window")),
            windows=tuple(w for w in (window(x) for x in data.get("windows") or ()) if w is not None),
            processes=tuple(str(p) for p in data.get("processes") or ()),
            cursor=_pair(data.get("cursor")),
            focused=str(data.get("focused", "")),
            selected=str(data.get("selected", "")),
            viewport=_pair(data.get("viewport")),
            browser=BrowserState(
                str(browser.get("url", "")),
                str(browser.get("title", "")),
                tuple(
                    TabInfo(str(t.get("id", "")), str(t.get("url", "")), str(t.get("title", "")), bool(t.get("active")))
                    for t in browser.get("tabs") or ()
                ),
                bool(browser.get("loading", False)),
                str(browser.get("dialog", "")),
            )
            if browser
            else None,
            elements=tuple(StateElement.from_dict(e) for e in data.get("elements") or ()),
            text=str(data.get("text", "")),
            ocr_text=str(data.get("ocr_text", "")),
            screenshot=ScreenshotRef(
                int(shot.get("width", 0)),
                int(shot.get("height", 0)),
                float(shot.get("scale", 1.0)),
                str(shot.get("sha256", "")),
                str(shot.get("path", "")),
                float(shot.get("captured_at", 0.0)),
            )
            if shot
            else None,
            perception=tuple(
                PerceptionRecord(
                    str(p.get("source", "")),
                    str(p.get("status", "")),
                    str(p.get("detail", "")),
                    float(p.get("seconds", 0.0)),
                    int(p.get("count", 0)),
                )
                for p in data.get("perception") or ()
            ),
            network=tuple(sorted((str(k), str(v)) for k, v in (data.get("network") or {}).items())),
            cwd=str(data.get("cwd", "")),
            metadata=tuple(sorted((str(k), str(v)) for k, v in (data.get("metadata") or {}).items())),
        )

    @classmethod
    def from_observation(cls, observation: Observation, *, surface: str = "browser") -> ComputerState:
        """A state from one provider's :class:`~highhx.computer.model.Observation`."""
        elements = tuple(StateElement.from_ui(e) for e in observation.elements)
        focused = next((e.id for e in observation.elements if e.focused), "")
        return cls(
            surface=surface,
            timestamp=observation.captured_at or time.time(),
            active_app=observation.application,
            browser=BrowserState(observation.url, observation.title) if surface == "browser" else None,
            active_window=WindowInfo("", observation.application, observation.title) if surface != "browser" else None,
            elements=elements,
            focused=focused,
            text=observation.text,
            perception=(PerceptionRecord(observation.provider, "ok", count=len(elements)),),
        )

    def to_observation(self) -> Observation:
        """The structured part of this state as an :class:`~highhx.computer.model.Observation`
        (what the existing runtime and selectors work on)."""
        from highhx.computer.model import Observation

        return Observation(
            provider=self.surface,
            application=self.active_app,
            title=self.title,
            url=self.url,
            elements=[e.to_ui() for e in self.elements],
            text=self.text,
            captured_at=self.timestamp,
        )
