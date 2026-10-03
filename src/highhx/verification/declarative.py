"""Declarative verification: what must be true after an action, as data.

    verification:
      any:                                   # all · any · not combine checks
        - absent: {role: button, name: Submit}
        - text: "Thank you"
        - url_contains: /done
        - network: {url_contains: /api/orders, status: 201}

Every check is ``satisfied``, ``unsatisfied`` or ``unknown``, and **unknown never counts as
success**. A check that cannot be decided from what is available (no screenshot for a visual
check, no network log, a secret field's value) is unknown, never guessed. With an ``observe``
callback the checks are re-sampled until they hold or ``timeout`` passes, so a slow page is not
mistaken for a failure.

=================  ==========================================================================
check              what it looks at
=================  ==========================================================================
exit_code          ``result.output["exit_code"]`` (or the action's own success)
file               ``{path, exists, contains, not_contains}`` under the project root
element / dom /    ``{role, name, exact, value, checked, enabled, focused}`` in the state's
accessibility      elements (dom: browser DOM only; accessibility: AX / Android only)
absent             an element matching ``{role, name}`` is *not* there
text / text_absent the state's visible text (structure and OCR)
url_contains /     the browser URL / title
url_matches /
title_contains
changed            the state's fingerprint differs from the one before the action
visual             screenshots before/after: ``{changed: true, min_ratio}``
screenshot         the state has a screenshot (``true``)
process            ``{name, running}`` in the state's processes
application        ``{name, active}``: the frontmost application
network            ``{url_contains, status, method}`` in the supplied network log
custom             ``{name, args}``: a check registered by the caller or a plugin
=================  ==========================================================================
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.errors import ValidationError

if TYPE_CHECKING:
    from highhx.actions.spec import ActionResult
    from highhx.execution.cancellation import CancellationToken
    from highhx.perception.state import ComputerState, StateElement


class Verdict(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"


CustomCheck = Callable[["VerificationContext", dict[str, Any]], tuple[bool | None, str]]


@dataclass
class VerificationContext:
    result: ActionResult | None = None
    before: ComputerState | None = None
    after: ComputerState | None = None
    root: Path | None = None
    observe: Callable[[], ComputerState] | None = None
    """Re-sample the state (for waiting until a check holds)."""
    network: list[dict[str, Any]] | None = None
    """Requests seen during the action: ``{url, method, status}``. None: no log is available."""
    custom: dict[str, CustomCheck] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    check: str
    verdict: Verdict
    detail: str = ""
    children: tuple[CheckResult, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.verdict == Verdict.SATISFIED

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.check, "verdict": str(self.verdict), "detail": self.detail}
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out


@dataclass(frozen=True)
class VerificationReport:
    verdict: Verdict
    result: CheckResult
    samples: int = 1
    seconds: float = 0.0

    @property
    def satisfied(self) -> bool:
        return self.verdict == Verdict.SATISFIED

    @property
    def partial(self) -> bool:
        """Some, but not all, of a top-level ``all`` held."""
        kids = self.result.children
        return (
            self.result.check == "all"
            and self.verdict != Verdict.SATISFIED
            and any(c.satisfied for c in kids)
            and any(c.verdict == Verdict.UNSATISFIED for c in kids)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "partial": self.partial,
            "samples": self.samples,
            "seconds": round(self.seconds, 3),
            "result": self.result.to_dict(),
        }


CHECKS: dict[str, Callable[[VerificationContext, Any], CheckResult]] = {}


def check(name: str) -> Callable[[Callable[[VerificationContext, Any], CheckResult]], Callable[..., CheckResult]]:
    def register(fn: Callable[[VerificationContext, Any], CheckResult]) -> Callable[..., CheckResult]:
        CHECKS[name] = fn
        return fn

    return register


def _ok(name: str, ok: bool | None, detail: str = "") -> CheckResult:
    verdict = Verdict.UNKNOWN if ok is None else (Verdict.SATISFIED if ok else Verdict.UNSATISFIED)
    return CheckResult(name, verdict, detail)


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


# ------------------------------------------------------------------ combinators
def evaluate(spec: Any, ctx: VerificationContext) -> CheckResult:
    """One evaluation of ``spec`` (a check mapping, or a list meaning ``all``)."""
    if isinstance(spec, list):
        spec = {"all": spec}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ValidationError("A verification check is a mapping with exactly one key.", details=[repr(spec)[:200]])
    name, value = next(iter(spec.items()))
    if name in ("all", "any"):
        if not isinstance(value, list) or not value:
            raise ValidationError(f"'{name}' needs a non-empty list of checks.")
        kids = tuple(evaluate(v, ctx) for v in value)
        verdicts = [k.verdict for k in kids]
        if name == "all":
            if Verdict.UNSATISFIED in verdicts:
                verdict = Verdict.UNSATISFIED
            elif Verdict.UNKNOWN in verdicts:
                verdict = Verdict.UNKNOWN
            else:
                verdict = Verdict.SATISFIED
        elif Verdict.SATISFIED in verdicts:
            verdict = Verdict.SATISFIED
        elif Verdict.UNKNOWN in verdicts:
            verdict = Verdict.UNKNOWN
        else:
            verdict = Verdict.UNSATISFIED
        held = sum(k.satisfied for k in kids)
        return CheckResult(name, verdict, f"{held}/{len(kids)} held", kids)
    if name == "not":
        inner = evaluate(value, ctx)
        flipped = {
            Verdict.SATISFIED: Verdict.UNSATISFIED,
            Verdict.UNSATISFIED: Verdict.SATISFIED,
            Verdict.UNKNOWN: Verdict.UNKNOWN,
        }[inner.verdict]
        return CheckResult("not", flipped, inner.detail, (inner,))
    fn = CHECKS.get(name)
    if fn is None:
        raise ValidationError(f"Unknown verification check {name!r}.", hint=f"Known: {', '.join(sorted(CHECKS))}")
    return fn(ctx, value)


def validate(spec: Any) -> list[str]:
    """Problems with a verification spec, without evaluating it."""
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            node = {"all": node}
        if not isinstance(node, dict) or len(node) != 1:
            problems.append(f"{path}: a check is a mapping with exactly one key")
            return
        name, value = next(iter(node.items()))
        if name in ("all", "any"):
            if not isinstance(value, list) or not value:
                problems.append(f"{path}.{name}: needs a non-empty list")
                return
            for i, child in enumerate(value):
                walk(child, f"{path}.{name}[{i}]")
        elif name == "not":
            walk(value, f"{path}.not")
        elif name not in CHECKS:
            problems.append(f"{path}: unknown check {name!r}")

    walk(spec, "verification")
    return problems


def verify(
    spec: Any,
    ctx: VerificationContext,
    *,
    timeout: float = 0.0,
    interval: float = 0.25,
    cancel: CancellationToken | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> VerificationReport:
    """Evaluate ``spec``; with ``timeout`` and ``ctx.observe``, re-sample until it is satisfied
    (an unsatisfied or unknown check is retried, and the last answer is reported)."""
    started = time.monotonic()
    samples = 1
    result = evaluate(spec, ctx)
    while (
        result.verdict != Verdict.SATISFIED
        and ctx.observe is not None
        and time.monotonic() - started < timeout
        and not (cancel is not None and cancel.cancelled)
    ):
        if cancel is not None:
            if cancel.wait(interval):
                break
        else:
            sleep(interval)
        ctx.after = ctx.observe()
        samples += 1
        result = evaluate(spec, ctx)
    return VerificationReport(result.verdict, result, samples, time.monotonic() - started)


# ----------------------------------------------------------------------- checks
@check("exit_code")
def _exit_code(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.result is None:
        return _ok("exit_code", None, "no action result")
    code = ctx.result.output.get("exit_code")
    if code is None:
        code = ctx.result.output.get("code")
    if code is None:
        return _ok("exit_code", ctx.result.ok == (int(value) == 0), f"no exit code; the action {'succeeded' if ctx.result.ok else 'failed'}")
    return _ok("exit_code", int(code) == int(value), f"exit code {code}")


@check("file")
def _file(ctx: VerificationContext, value: Any) -> CheckResult:
    if not isinstance(value, dict) or not value.get("path"):
        raise ValidationError("'file' needs a path.")
    path = Path(str(value["path"])).expanduser()
    if not path.is_absolute():
        if ctx.root is None:
            return _ok("file", None, "no project root to resolve the path against")
        path = ctx.root / path
    exists = path.exists()
    if "exists" in value and bool(value["exists"]) != exists:
        return _ok("file", False, f"{path.name} {'exists' if exists else 'does not exist'}")
    if not exists:
        return _ok("file", value.get("exists", True) is False, f"{path.name} does not exist")
    if "contains" in value or "not_contains" in value:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _ok("file", None, f"cannot read {path.name}: {exc}")
        if "contains" in value and str(value["contains"]) not in text:
            return _ok("file", False, f"{path.name} does not contain {value['contains']!r}")
        if "not_contains" in value and str(value["not_contains"]) in text:
            return _ok("file", False, f"{path.name} contains {value['not_contains']!r}")
    return _ok("file", True, f"{path.name} ok")


_SOURCE_FILTERS = {"element": None, "dom": ("dom",), "accessibility": ("ax", "android")}


def _matching(state: ComputerState, value: dict[str, Any], sources: tuple[str, ...] | None) -> list[StateElement]:
    role = value.get("role")
    name = value.get("name")
    exact = bool(value.get("exact", False))
    out = []
    for e in state.elements:
        if sources and not set(e.sources) & set(sources):
            continue
        if role and e.role != role:
            continue
        if name is not None:
            have, want = _norm(e.name), _norm(name)
            if (have != want) if exact else (want not in have):
                continue
        out.append(e)
    return out


def _element_check(kind: str) -> Callable[[VerificationContext, Any], CheckResult]:
    def run(ctx: VerificationContext, value: Any) -> CheckResult:
        if isinstance(value, str):
            value = {"name": value}
        if ctx.after is None:
            return _ok(kind, None, "no observation after the action")
        found = _matching(ctx.after, value, _SOURCE_FILTERS[kind])
        what = f"{value.get('role') or 'element'} {value.get('name')!r}" if value.get("name") else str(value.get("role"))
        if not found:
            return _ok(kind, False, f"no {what}")
        element = found[int(value.get("index", 0))] if int(value.get("index", 0)) < len(found) else found[0]
        for attr in ("value", "checked", "enabled", "focused"):
            if attr not in value:
                continue
            if attr == "value" and element.secret:
                return _ok(kind, None, "a secret field's value cannot be read")
            have = getattr(element, attr)
            if have is None:
                return _ok(kind, None, f"{what}: {attr} is not reported")
            if have != value[attr]:
                return _ok(kind, False, f"{what}: {attr} is {have!r}, expected {value[attr]!r}")
        return _ok(kind, True, f"found {element.label()}")

    return run


for _kind in _SOURCE_FILTERS:
    CHECKS[_kind] = _element_check(_kind)


@check("absent")
def _absent(ctx: VerificationContext, value: Any) -> CheckResult:
    if isinstance(value, str):
        value = {"name": value}
    if ctx.after is None:
        return _ok("absent", None, "no observation after the action")
    found = _matching(ctx.after, value, None)
    return _ok("absent", not found, f"{len(found)} matching element(s)")


@check("text")
def _text(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.after is None:
        return _ok("text", None, "no observation after the action")
    names = " ".join(e.name for e in ctx.after.elements)
    haystack = _norm(f"{ctx.after.all_text} {names}")
    return _ok("text", _norm(value) in haystack, f"{value!r} {'is' if _norm(value) in haystack else 'is not'} shown")


@check("text_absent")
def _text_absent(ctx: VerificationContext, value: Any) -> CheckResult:
    inner = _text(ctx, value)
    flipped = {Verdict.SATISFIED: False, Verdict.UNSATISFIED: True}.get(inner.verdict)
    return _ok("text_absent", flipped, inner.detail)


@check("url_contains")
def _url_contains(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.after is None or ctx.after.browser is None:
        return _ok("url_contains", None, "no browser state")
    return _ok("url_contains", str(value) in ctx.after.url, ctx.after.url)


@check("url_matches")
def _url_matches(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.after is None or ctx.after.browser is None:
        return _ok("url_matches", None, "no browser state")
    return _ok("url_matches", re.search(str(value), ctx.after.url) is not None, ctx.after.url)


@check("title_contains")
def _title_contains(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.after is None:
        return _ok("title_contains", None, "no observation after the action")
    return _ok("title_contains", _norm(value) in _norm(ctx.after.title), ctx.after.title)


@check("changed")
def _changed(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.before is None or ctx.after is None:
        return _ok("changed", None, "needs observations before and after")
    changed = ctx.before.fingerprint() != ctx.after.fingerprint()
    return _ok("changed", changed == bool(value), "the screen changed" if changed else "no visible change")


@check("visual")
def _visual(ctx: VerificationContext, value: Any) -> CheckResult:
    from highhx.perception.diff import VisualDiff

    value = value if isinstance(value, dict) else {"changed": bool(value)}
    if ctx.before is None or ctx.after is None or ctx.before.screenshot is None or ctx.after.screenshot is None:
        return _ok("visual", None, "needs screenshots before and after")
    diff = VisualDiff(min_ratio=float(value.get("min_ratio", 0.002))).compare(ctx.before.screenshot, ctx.after.screenshot)
    if diff.method == "digest" and diff.detail and "not available" in diff.detail:
        return _ok("visual", None, diff.detail)
    want = bool(value.get("changed", True))
    return _ok("visual", diff.changed == want, f"{diff.ratio:.1%} of the screen changed ({diff.method})")


@check("screenshot")
def _screenshot(ctx: VerificationContext, value: Any) -> CheckResult:
    if ctx.after is None:
        return _ok("screenshot", None, "no observation")
    return _ok("screenshot", (ctx.after.screenshot is not None) == bool(value), "")


@check("process")
def _process(ctx: VerificationContext, value: Any) -> CheckResult:
    value = value if isinstance(value, dict) else {"name": str(value)}
    if ctx.after is None or not ctx.after.processes:
        return _ok("process", None, "processes are not observed here")
    running = any(_norm(value["name"]) == _norm(p) for p in ctx.after.processes)
    want = bool(value.get("running", True))
    return _ok("process", running == want, f"{value['name']} {'is' if running else 'is not'} running")


@check("application")
def _application(ctx: VerificationContext, value: Any) -> CheckResult:
    value = value if isinstance(value, dict) else {"name": str(value)}
    if ctx.after is None or not ctx.after.active_app:
        return _ok("application", None, "the frontmost application is not observed")
    active = _norm(value["name"]) in _norm(ctx.after.active_app)
    if value.get("active", True):
        return _ok("application", active, f"frontmost: {ctx.after.active_app}")
    return _ok("application", not active, f"frontmost: {ctx.after.active_app}")


@check("network")
def _network(ctx: VerificationContext, value: Any) -> CheckResult:
    if not isinstance(value, dict):
        raise ValidationError("'network' needs {url_contains, status, method}.")
    if ctx.network is None:
        return _ok("network", None, "no network log was recorded for this action")
    for request in ctx.network:
        if value.get("url_contains") and str(value["url_contains"]) not in str(request.get("url", "")):
            continue
        if value.get("method") and str(value["method"]).upper() != str(request.get("method", "")).upper():
            continue
        if value.get("status") is not None and int(value["status"]) != int(request.get("status") or 0):
            continue
        return _ok("network", True, f"{request.get('method', '')} {request.get('url', '')} → {request.get('status')}")
    return _ok("network", False, f"no matching request among {len(ctx.network)}")


@check("custom")
def _custom(ctx: VerificationContext, value: Any) -> CheckResult:
    name = value.get("name") if isinstance(value, dict) else str(value)
    fn = ctx.custom.get(str(name))
    if fn is None:
        return _ok("custom", None, f"no custom check named {name!r} is registered")
    ok, detail = fn(ctx, value if isinstance(value, dict) else {})
    return _ok("custom", ok, detail)
