"""Checked observation: bounded predicates about one exact window, evaluated from fresh state.

    driver.verify_state(window=4211, expect=[
        {"window": {"bounds": {"x": 0, "y": 25, "width": 1200, "height": 800}}},
        {"element": {"selector": {"role": "button", "label_contains": "Save"}, "enabled": False}},
    ])

Each predicate is ``satisfied``, ``unsatisfied`` or ``unknown`` — and ``unknown`` never counts as
success: a property the platform does not report (a secret field's value, selection state that is
not exposed) or an element absent from a bounded accessibility walk is unknown, not false. The
predicates are ANDed and re-sampled until they hold for ``stable_samples`` consecutive samples or
``timeout_ms`` passes, so a transient state is not mistaken for the result.

Element predicates are evaluated against the accessibility tree of the window's application
(HighhX's trees are per application, not per window). Nothing here acts on the computer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.automation.engine.bridge import EngineError
from highhx.core.errors import UsageError
from highhx.utils.validation import Bool, Int, List, Obj, Prop, Str

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver, Window
    from highhx.computer.model import Observation, UIElement

MAX_PREDICATES = 8
MAX_TIMEOUT_MS = 10_000
SAMPLE_INTERVAL = 0.2
TREE_LIMIT = 300

_BOUNDS = Obj(
    {
        "x": Prop(Int(minimum=-100_000, maximum=100_000), required=True),
        "y": Prop(Int(minimum=-100_000, maximum=100_000), required=True),
        "width": Prop(Int(minimum=0, maximum=100_000), required=True),
        "height": Prop(Int(minimum=0, maximum=100_000), required=True),
        "tolerance": Prop(Int(minimum=0, maximum=100), description="Points of slack per edge (default 4)."),
    }
)
_ELEMENT = Obj(
    {
        "selector": Prop(
            Obj(
                {"role": Prop(Str(min_length=1)), "label_contains": Prop(Str(min_length=1))},
                check=lambda v: [] if v else ["give a role, label_contains or both"],
            ),
            required=True,
        ),
        "exists": Prop(Bool(), description="Only true: absence cannot be proven from a bounded walk."),
        "value_equals": Prop(Str()),
        "enabled": Prop(Bool()),
        "selected": Prop(Bool()),
    },
    check=lambda v: (
        ["exists: false is not provable (the accessibility walk is bounded)"] if v.get("exists") is False else []
    ),
)
_TEXT = Obj(
    {"contains": Prop(Str(min_length=1, check=lambda v: "at most 200 characters" if len(v) > 200 else None), required=True)},
    description="Text visible in the window: its accessibility text, else OCR of the window (when installed).",
)
PREDICATE = Obj(
    {
        "window": Prop(Obj({"exists": Prop(Bool()), "bounds": Prop(_BOUNDS)})),
        "element": Prop(_ELEMENT),
        "text": Prop(_TEXT),
    },
    check=lambda v: [] if len([k for k in v if v[k] is not None]) == 1 else ["give exactly one of window, element or text"],
)
PREDICATES = List(PREDICATE, min_items=1, description=f"1 to {MAX_PREDICATES} predicates, all of which must hold.")


@dataclass
class PredicateResult:
    index: int
    status: str
    """satisfied | unsatisfied | unknown"""
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "status": self.status, "detail": self.detail}


@dataclass
class StateCheck:
    status: str
    """satisfied (every predicate, stably) | unsatisfied | unknown"""
    predicates: list[PredicateResult]
    samples: int
    elapsed_ms: int
    window: dict[str, Any] | None = field(default=None)

    @property
    def ok(self) -> bool:
        return self.status == "satisfied"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "predicates": [p.to_dict() for p in self.predicates],
            "samples": self.samples,
            "elapsed_ms": self.elapsed_ms,
            "window": self.window,
        }


def check_predicates(expect: Any) -> list[dict[str, Any]]:
    """``expect`` as a list of valid predicates, or :class:`UsageError` naming what is wrong."""
    problems = PREDICATES.validate(expect, "expect")
    if not problems and len(expect) > MAX_PREDICATES:
        problems = [f"expect: at most {MAX_PREDICATES} predicates"]
    if problems:
        raise UsageError("Invalid verification predicates.", details=problems)
    return [dict(p) for p in expect]


def combine(results: list[PredicateResult]) -> str:
    statuses = {r.status for r in results}
    if "unsatisfied" in statuses:
        return "unsatisfied"
    return "unknown" if "unknown" in statuses else "satisfied"


def _window(spec: dict[str, Any], window: Window | None) -> tuple[str, str]:
    if spec.get("exists") is not None:
        wanted = bool(spec["exists"])
        if (window is not None) != wanted:
            return "unsatisfied", "the window is " + ("gone" if wanted else "still open")
        if not wanted:
            return "satisfied", "the window is gone"
    if spec.get("bounds") is not None:
        if window is None:
            return "unsatisfied", "the window is gone"
        b = spec["bounds"]
        slack = int(b.get("tolerance", 4))
        actual = (window.x, window.y, window.width, window.height)
        wanted_frame = (b["x"], b["y"], b["width"], b["height"])
        if any(abs(a - w) > slack for a, w in zip(actual, wanted_frame, strict=True)):
            return "unsatisfied", f"the window is at {list(actual)}, not {list(wanted_frame)}"
        return "satisfied", f"the window is at {list(actual)}"
    return "satisfied", "the window exists"


def _matches(element: UIElement, selector: dict[str, Any]) -> bool:
    role = selector.get("role")
    label = str(selector.get("label_contains") or "").lower()
    return (role is None or element.role == role) and (not label or label in element.name.lower())


def _element(spec: dict[str, Any], tree: Observation | str) -> tuple[str, str]:
    if isinstance(tree, str):
        return "unknown", tree  # the tree could not be read: why
    found = [e for e in tree.elements if _matches(e, spec["selector"])]
    if not found:
        return "unknown", "no matching element in the observed tree (a bounded walk cannot prove absence)"
    checks = [k for k in ("value_equals", "enabled", "selected") if spec.get(k) is not None]
    if not checks:
        return "satisfied", f"{len(found)} matching element(s)"
    unknown = False
    observed: list[str] = []
    for element in found:
        verdicts: list[bool | None] = []
        for key in checks:
            if key == "value_equals":
                verdicts.append(None if element.secret else element.value == spec[key])
                observed.append("value hidden" if element.secret else f"value {element.value[:60]!r}")
            elif key == "enabled":
                verdicts.append(element.enabled == spec[key])
                observed.append("enabled" if element.enabled else "disabled")
            else:
                verdicts.append(None if element.checked is None else element.checked == spec[key])
                observed.append("selection not reported" if element.checked is None else f"selected={element.checked}")
        if all(v is True for v in verdicts):
            return "satisfied", f"{element.role} {element.name!r}: " + ", ".join(observed[-len(checks) :])
        unknown = unknown or (None in verdicts and False not in verdicts)
    detail = "; ".join(dict.fromkeys(observed))
    return ("unknown" if unknown else "unsatisfied"), detail


def _text(driver: HighhXDriver, window: Window, wanted: str, tree: Observation | str) -> tuple[str, str]:
    """Visible text: the window's accessibility names and values first; OCR of the window's own
    capture when that finds nothing and tesseract is installed. Not found stays unknown."""
    needle = " ".join(wanted.lower().split())
    if not isinstance(tree, str):
        for element in tree.elements:
            haystack = " ".join(f"{element.name} {'' if element.secret else element.value}".lower().split())
            if needle in haystack:
                return "satisfied", f"{element.role} {element.name!r} shows it"
    from highhx.computer.desktop import TesseractOCR

    reader = TesseractOCR()
    if not reader.capability().available:
        return "unknown", "not in the accessibility text, and OCR is not installed to read the pixels"
    try:
        shot = driver.screenshot(window.id)
    except EngineError as exc:
        return "unknown", f"not in the accessibility text; the window could not be captured: {exc.message}"
    try:
        text = " ".join(reader.read_image(shot.path).text.lower().split())
    finally:
        shot.path.unlink(missing_ok=True)
    if needle in text:
        return "satisfied", "read on screen (OCR)"
    return "unknown", "neither the accessibility text nor OCR shows it (absence is not proven)"


def sample(driver: HighhXDriver, window_id: int, expect: list[dict[str, Any]]) -> tuple[list[PredicateResult], Any]:
    """One fresh evaluation of every predicate."""
    window = next((w for w in driver.windows() if w.id == window_id), None)
    tree: Observation | str | None = None
    results: list[PredicateResult] = []
    for index, predicate in enumerate(expect):
        if predicate.get("window") is not None:
            status, detail = _window(predicate["window"], window)
        elif predicate.get("text") is not None:
            if window is None:
                status, detail = "unknown", "the window is gone, so its text cannot be read"
            else:
                if tree is None:
                    try:
                        tree = driver.get_ui_tree(window.app, limit=TREE_LIMIT)
                    except EngineError as exc:
                        tree = f"the accessibility tree could not be read: {exc.message}"
                status, detail = _text(driver, window, str(predicate["text"]["contains"]), tree)
        elif window is None:
            status, detail = "unknown", "the window is gone, so its elements cannot be observed"
        else:
            if tree is None:
                try:
                    tree = driver.get_ui_tree(window.app, limit=TREE_LIMIT)
                except EngineError as exc:
                    tree = f"the accessibility tree could not be read: {exc.message}"
            status, detail = _element(predicate["element"], tree)
        results.append(PredicateResult(index, status, detail))
    return results, window


def verify_state(
    driver: HighhXDriver,
    window: int,
    expect: Any,
    *,
    timeout_ms: int = 5000,
    stable_samples: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> StateCheck:
    """Sample until every predicate holds ``stable_samples`` times in a row, or time runs out."""
    predicates = check_predicates(expect)
    if not 0 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise UsageError(f"timeout_ms must be 0 to {MAX_TIMEOUT_MS}")
    if not 1 <= stable_samples <= 5:
        raise UsageError("stable_samples must be 1 to 5")
    needed = 1 if timeout_ms == 0 else stable_samples
    budget = 1 + int(timeout_ms / 1000 / SAMPLE_INTERVAL)  # also bounds a loop whose clock does not move
    started = time.monotonic()
    samples = streak = 0
    while True:
        results, found = sample(driver, window, predicates)
        samples += 1
        streak = streak + 1 if combine(results) == "satisfied" else 0
        elapsed = time.monotonic() - started
        if streak >= needed or samples >= budget or elapsed * 1000 >= timeout_ms:
            break
        sleep(SAMPLE_INTERVAL)
    status = combine(results)
    if status == "satisfied" and streak < needed:
        status = "unknown"  # it held at the end, but not for long enough to call it stable
        results = [*results, PredicateResult(len(results), "unknown", f"held for {streak} of {needed} samples")]
    return StateCheck(
        status,
        results,
        samples,
        round((time.monotonic() - started) * 1000),
        found.__dict__ if found is not None else None,
    )
