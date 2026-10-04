"""Browser workflows: record what a person does, store it semantically, replay it with self-healing.

    highhx browser record invoices --url https://app.example.com
      the HighhX browser opens. Clicks, typing, selections and Enter are captured by a script
      that labels each element with the *same* role and accessible-name rules as observation
      (OBSERVE_JS), plus its stable attributes (id, data-testid, name, href, classes), its box
      and the viewport. Each event becomes a step:
        {"action": "click", "intent": "click link Invoices",
         "target": {"label": "Invoices", "role": "link",
                    "accessibility": …, "dom": {"href": "/invoices", …}, "text": …,
                    "visual": {"box": …}, "coordinate": {"x": …, "url": …}},
         "verify": {"url_contains": "/invoices"}}
      Text typed into password, card or one-time-code fields is never recorded. It becomes a
      variable that the replay asks for (``--var NAME=value`` or ``HIGHHX_VAR_NAME``).

    highhx browser replay invoices
      the steps run through the agent loop: each target is grounded on the current page
      (accessibility → DOM → text → OCR → vision → coordinates), each action goes through the
      executor (risk, policy, approval, audit), each step is verified. A step whose target was
      found by a different representation than recorded is *healed*: its selectors are refreshed
      from the element found and saved back, with the heal remembered in the step's history.

Coordinates are stored but are the last resort, and are used only on the same page in the same
viewport. A workflow never replays as a list of clicks at points.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from highhx.core.errors import NotFoundError, UsageError, ValidationError
from highhx.grounding.selectors import Target
from highhx.perception.state import BrowserState, ComputerState, StateElement

if TYPE_CHECKING:
    from highhx.computer.browser import ChromeBrowser
    from highhx.execution.cancellation import CancellationToken
    from highhx.security.secrets import Redactor

BINDING = "__highhxRecord"
NAVIGATION_GRACE = 2.5
"""A navigation this soon after a click belongs to the click (its result), not a new step."""
_NAME = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
_VARIABLE = re.compile(r"[^A-Z0-9]+")


def _helpers() -> str:
    from highhx.computer.browser import OBSERVE_JS

    start = OBSERVE_JS.index("const secretInput")
    end = OBSERVE_JS.index("const out = [];")
    return OBSERVE_JS[start:end]


RECORD_JS = r"""
(() => {
  if (window.__highhxRecorder) return;
  window.__highhxRecorder = true;
  HELPERS
  const actionable = e => {
    let n = e;
    while (n && n !== document.body && n.nodeType === 1 && !roleOf(n)) n = n.parentElement;
    return n && n !== document.body && n.nodeType === 1 ? n : null;
  };
  const describe = n => {
    const r = n.getBoundingClientRect();
    return {
      role: roleOf(n), name: nameOf(n).slice(0, 160), secret: n.tagName === 'INPUT' && secretInput(n),
      attributes: {
        tag: n.tagName.toLowerCase(), type: (n.getAttribute('type') || '').toLowerCase(),
        href: n.getAttribute('href') || '', class: (n.getAttribute('class') || '').slice(0, 120),
        name: n.getAttribute('name') || '', dom_id: (n.id || '').slice(0, 80),
        testid: (n.getAttribute('data-testid') || n.getAttribute('data-test') || n.getAttribute('data-cy') || '').slice(0, 80),
        autocomplete: n.getAttribute('autocomplete') || '',
      },
      bounds: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    };
  };
  const send = (kind, target, extra) => {
    const n = actionable(target);
    if (!n) return;
    try {
      window.__highhxRecord(JSON.stringify(Object.assign({kind, url: location.href, title: document.title,
        viewport: [innerWidth, innerHeight], element: describe(n), at: Date.now()}, extra || {})));
    } catch (_) {}
  };
  document.addEventListener('click', ev => send('click', ev.target), true);
  document.addEventListener('change', ev => {
    const t = ev.target;
    if (!t || !t.tagName) return;
    if (t.tagName === 'SELECT') { send('select', t, {option: (t.options[t.selectedIndex] || {}).text || t.value}); return; }
    if (t.type === 'checkbox' || t.type === 'radio' || t.type === 'file') return;
    send('type', t, {text: secretInput(t) ? null : String(t.value ?? '')});
  }, true);
  document.addEventListener('focusout', ev => {
    const t = ev.target;
    if (t && t.isContentEditable) send('type', t, {text: String(t.innerText || '')});
  }, true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Enter') send('press', ev.target, {key: 'enter'}); }, true);
})()
"""


def record_script() -> str:
    return RECORD_JS.replace("HELPERS", _helpers())


# ------------------------------------------------------------------- the model
@dataclass
class WorkflowStep:
    action: str
    """click · type · select · press · open"""
    intent: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] | list[Any] | None = None
    url: str = ""
    screenshot: str = ""
    grounding: list[str] = field(default_factory=lambda: ["accessibility", "dom", "text", "ocr", "vision", "coordinate"])
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "intent": self.intent}
        for key in ("target", "parameters", "verify", "url", "screenshot", "grounding", "history"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowStep:
        return cls(
            action=str(data["action"]),
            intent=str(data.get("intent") or ""),
            target=dict(data.get("target") or {}),
            parameters=dict(data.get("parameters") or {}),
            verify=data.get("verify"),
            url=str(data.get("url") or ""),
            screenshot=str(data.get("screenshot") or ""),
            grounding=list(data.get("grounding") or ["accessibility", "dom", "text", "ocr", "vision", "coordinate"]),
            history=list(data.get("history") or []),
        )


@dataclass
class BrowserWorkflow:
    name: str
    start_url: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    """Values the replay must be given (secret fields are never recorded)."""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    heals: int = 0
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "start_url": self.start_url,
            "variables": self.variables,
            "created": self.created,
            "updated": self.updated,
            "heals": self.heals,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrowserWorkflow:
        if int(data.get("version", 1)) > 1:
            raise ValidationError("This workflow was written by a newer HighhX.")
        return cls(
            name=str(data["name"]),
            start_url=str(data.get("start_url") or ""),
            steps=[WorkflowStep.from_dict(s) for s in data.get("steps") or []],
            variables=list(data.get("variables") or []),
            created=float(data.get("created") or time.time()),
            updated=float(data.get("updated") or time.time()),
            heals=int(data.get("heals") or 0),
        )

    def script(self, variables: dict[str, str]) -> list[dict[str, Any]]:
        """Steps for the agent loop's ScriptedPlanner (variables filled in)."""
        missing = [v for v in self.variables if v not in variables]
        if missing:
            raise UsageError(
                f"The workflow needs: {', '.join(missing)} (never recorded: they went into secret fields).",
                hint=" ".join(f"--var {m}=…" for m in missing) + f"  (or HIGHHX_VAR_{missing[0]})",
            )
        out: list[dict[str, Any]] = []
        if self.start_url:
            out.append({"action": "open", "parameters": {"url": self.start_url}, "intent": f"open {self.start_url}", "verify": {"url_contains": urlparse(self.start_url).hostname or self.start_url}})
        for step in self.steps:
            params = dict(step.parameters)
            if "variable" in params:
                params = {k: v for k, v in params.items() if k != "variable"} | {"text": variables[params["variable"]]}
            out.append({"action": step.action, "intent": step.intent, "target": step.target, "parameters": params, **({"verify": step.verify} if step.verify else {})})
        return out


def variable_name(label: str) -> str:
    return _VARIABLE.sub("_", label.upper()).strip("_")[:40] or "SECRET"


# ---------------------------------------------------------------- events → steps
def _element(data: dict[str, Any], index: int) -> StateElement:
    attributes = {k: str(v) for k, v in (data.get("attributes") or {}).items() if v}
    if data.get("secret"):
        attributes["type"] = "password"
    bounds = data.get("bounds")
    return StateElement(
        f"r{index}",
        str(data.get("role") or ""),
        str(data.get("name") or ""),
        bounds=tuple(int(v) for v in bounds) if bounds and len(bounds) == 4 else None,  # type: ignore[arg-type]
        sources=("dom",),
        attributes=tuple(sorted(attributes.items())),
    )


def steps_from_events(events: Sequence[dict[str, Any]], start_url: str = "") -> tuple[list[WorkflowStep], list[str]]:
    """Recorded browser events → semantic steps (and the variables secret fields need).

    A click into a text field followed by typing into it is one ``type`` step. A navigation that
    follows a click is that click's result (its check), and any other navigation is an ``open``
    step.
    """
    steps: list[WorkflowStep] = []
    variables: list[str] = []
    last_click_at = 0.0
    previous_url = start_url
    for index, event in enumerate(events):
        kind = event.get("kind")
        if kind == "navigate":
            url = str(event.get("url") or "")
            if not url or url == previous_url:
                continue
            caused = steps and steps[-1].action in ("click", "press") and float(event.get("at", 0)) - last_click_at <= NAVIGATION_GRACE * 1000
            if caused:
                path = urlparse(url).path or "/"
                steps[-1].verify = {"url_contains": path if path != "/" else urlparse(url).hostname or url}
            elif not steps and not start_url:
                start_url = url
            else:
                steps.append(WorkflowStep("open", f"open {url}", parameters={"url": url}, verify={"url_contains": urlparse(url).hostname or url}, url=previous_url))
            previous_url = url
            continue
        data = event.get("element") or {}
        element = _element(data, index)
        page = ComputerState(
            "browser",
            browser=BrowserState(str(event.get("url") or ""), str(event.get("title") or "")),
            elements=(element,),
            viewport=(int(event["viewport"][0]), int(event["viewport"][1])) if event.get("viewport") else None,
            active_app="Chrome",
        )
        target = Target.from_element(element, page, description=f"the {element.role} labelled {element.name!r}").to_dict()
        label = f"{element.role} {element.name!r}" if element.name else element.role
        if kind == "click":
            last_click_at = float(event.get("at", 0))
            steps.append(WorkflowStep("click", f"click {label}", target, verify={"changed": True}, url=str(event.get("url") or "")))
        elif kind == "type":
            if steps and steps[-1].action == "click" and steps[-1].target.get("label") == element.name and steps[-1].target.get("role") == element.role:
                steps.pop()  # the click only focused the field
            text = event.get("text")
            if element.secret or text is None:
                name = variable_name(element.name or "secret")
                if name not in variables:
                    variables.append(name)
                params: dict[str, Any] = {"variable": name}
                verify = None
            else:
                params = {"text": str(text)}
                verify = {"element": {"name": element.name, "value": str(text)}} if element.name else None
            steps.append(WorkflowStep("type", f"type into {label}", target, params, verify, url=str(event.get("url") or "")))
        elif kind == "select":
            steps.append(WorkflowStep("select", f"choose {event.get('option')!r} in {label}", target, {"option": str(event.get("option") or "")}, {"changed": True}, url=str(event.get("url") or "")))
        elif kind == "press":
            if steps and steps[-1].action == "type" and steps[-1].target.get("label") == element.name:
                last_click_at = float(event.get("at", 0))
            steps.append(WorkflowStep("press", "press enter", {}, {"key": str(event.get("key") or "enter")}, {"changed": True}, url=str(event.get("url") or "")))
            last_click_at = float(event.get("at", 0))
        previous_url = str(event.get("url") or previous_url)
    return steps, variables


# ------------------------------------------------------------------ recording
class BrowserRecorder:
    """Captures a person's actions in the HighhX browser through a DevTools binding. Read-only:
    the recorder never acts on the page."""

    def __init__(self, browser: ChromeBrowser, *, cancel: CancellationToken | None = None) -> None:
        self.browser = browser
        self.cancel = cancel
        self.events: list[dict[str, Any]] = []
        self._conn: Any = None
        self._session = ""
        self._listener: Callable[[dict[str, Any]], None] | None = None

    def start(self, url: str | None = None) -> None:
        """Listen to the working tab. ``url`` is the page the caller already opened *through the
        executor* (``browser.open``): it is recorded as the start, never navigated here — the
        recorder only listens."""
        conn, session = self.browser._page(self.cancel)  # the working tab's DevTools session
        self._conn, self._session = conn, session
        script = record_script()
        conn.call("Runtime.addBinding", {"name": BINDING}, session_id=session, cancel=self.cancel)
        conn.call("Page.addScriptToEvaluateOnNewDocument", {"source": script}, session_id=session, cancel=self.cancel)
        conn.call("Runtime.evaluate", {"expression": script}, session_id=session, cancel=self.cancel)

        def listen(message: dict[str, Any]) -> None:
            if message.get("sessionId", "") != session:
                return
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Runtime.bindingCalled" and params.get("name") == BINDING:
                try:
                    event = json.loads(params.get("payload") or "{}")
                except ValueError:
                    return
                if isinstance(event, dict) and event.get("kind") in ("click", "type", "select", "press"):
                    self.events.append(event)
            elif method == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
                frame = params.get("frame") or {}
                self.events.append({"kind": "navigate", "url": str(frame.get("url") or ""), "at": time.time() * 1000})

        self._listener = listen
        conn.listeners.append(listen)
        if url:
            self.events.append({"kind": "navigate", "url": url, "at": time.time() * 1000})

    def poll(self, seconds: float = 0.25) -> int:
        """Process browser events for ``seconds``. Returns how many events have been recorded."""
        if self._conn is not None:
            self._conn.pump(cancel=self.cancel, seconds=seconds)
        return len(self.events)

    def stop(self) -> None:
        if self._conn is not None and self._listener is not None and self._listener in self._conn.listeners:
            self._conn.listeners.remove(self._listener)
        self._listener = None

    def workflow(self, name: str, start_url: str = "") -> BrowserWorkflow:
        steps, variables = steps_from_events(self.events, start_url)
        first = next((e.get("url") for e in self.events if e.get("kind") == "navigate"), "")
        return BrowserWorkflow(name, start_url or str(first or ""), steps, variables)


# --------------------------------------------------------------------- storage
class WorkflowStore:
    def __init__(self, root: Path, *, redactor: Redactor | None = None) -> None:
        self.root = root
        self.redactor = redactor

    @classmethod
    def for_app(cls, app: Any) -> WorkflowStore:
        from highhx.utils.paths import user_data_dir

        root = (app.root / ".highhx" / "browser-workflows") if getattr(app, "initialized", False) else user_data_dir() / "browser-workflows"
        return cls(root, redactor=app.redactor)

    def path(self, name: str) -> Path:
        if not _NAME.match(name):
            raise UsageError(f"Invalid workflow name {name!r} (letters, digits, '.', '-', '_').")
        return self.root / f"{name}.json"

    def save(self, workflow: BrowserWorkflow) -> Path:
        from highhx.utils.filesystem import atomic_write_text

        workflow.updated = time.time()
        text = json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False)
        if self.redactor is not None:
            text = self.redactor.redact(text)
        path = self.path(workflow.name)
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
        return path

    def load(self, name: str) -> BrowserWorkflow:
        path = self.path(name)
        if not path.is_file():
            raise NotFoundError(f"No browser workflow named {name!r}.", hint="See `highhx browser workflows`.")
        return BrowserWorkflow.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.is_dir() else []

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)


def variables_from(pairs: Sequence[str], needed: Sequence[str]) -> dict[str, str]:
    """``NAME=value`` pairs, then ``HIGHHX_VAR_<NAME>`` from the environment."""
    values: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise UsageError(f"Expected NAME=value, not {pair!r}.")
        values[key.strip().upper()] = value
    for name in needed:
        if name not in values and f"HIGHHX_VAR_{name}" in os.environ:
            values[name] = os.environ[f"HIGHHX_VAR_{name}"]
    return values


# --------------------------------------------------------------------- replay
@dataclass
class ReplayReport:
    status: str
    summary: str
    task_id: str
    healed: list[dict[str, Any]] = field(default_factory=list)
    saved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "task_id": self.task_id, "healed": self.healed, "saved": self.saved}


def heal_workflow(workflow: BrowserWorkflow, trajectory: Any) -> list[dict[str, Any]]:
    """Refresh the selectors of steps whose target was found by another representation than its
    recorded accessibility one. Returns what changed."""
    healed: list[dict[str, Any]] = []
    offset = 1 if workflow.start_url else 0
    successful: dict[str, Any] = {}
    for tstep in trajectory.steps:
        if tstep.outcome not in ("success", "partial_success") or not tstep.grounding:
            continue
        intent = (tstep.action.get("step") or {}).get("intent") or tstep.intent
        successful.setdefault(intent, tstep)
    for index, step in enumerate(workflow.steps):
        tstep = successful.get(step.intent)
        if tstep is None:
            continue
        grounding = tstep.grounding or {}
        strategy = (grounding.get("candidate") or {}).get("strategy", "")
        fresh = grounding.get("target") or {}
        if not fresh or (strategy == "accessibility" and fresh.get("label") == step.target.get("label")):
            continue
        changes = [k for k in ("label", "accessibility", "dom", "text", "coordinate") if fresh.get(k) != step.target.get(k)]
        if not changes:
            continue
        candidate = grounding.get("candidate") or {}
        failed = [a.get("strategy") for a in grounding.get("attempts") or [] if a.get("result") != "success"]
        entry = {
            "step": index + offset,
            "strategy": strategy,
            "changed": changes,
            "was": step.target.get("label"),
            "now": fresh.get("label"),
            "old_selector": {k: step.target.get(k) for k in changes},
            "new_selector": {k: fresh.get(k) for k in changes},
            "reason": f"{', '.join(str(f) for f in failed) or 'the recorded selector'} no longer matched; found by {strategy}",
            "confidence": round(float(candidate.get("score") or candidate.get("confidence") or 0.0), 3),
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        step.history.append(entry)
        step.target = {**fresh, "semantic": step.target.get("semantic") or fresh.get("semantic")}
        if step.verify and isinstance(step.verify, dict) and "element" in step.verify and fresh.get("label"):
            step.verify = {"element": {**step.verify["element"], "name": fresh["label"]}}
        healed.append(entry)
    if healed:
        workflow.heals += len(healed)
    return healed


def replay(
    executor: Any,
    workflow: BrowserWorkflow,
    *,
    variables: dict[str, str] | None = None,
    workflows: WorkflowStore | None = None,
    trajectories: Any = None,
    save_heals: bool = True,
    traces: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplayReport:
    """Run ``workflow`` through the agent loop (grounding, executor, verification, recovery) and
    save the selectors it had to heal."""
    from highhx.agent.loop import AgentLoop, AgentTask, ScriptedPlanner

    values = dict(variables or {})
    if values:
        executor.app.redactor.add([v for k, v in values.items() if k in workflow.variables])
    script = workflow.script(values)
    task = AgentTask(f"replay browser workflow {workflow.name}", surface="browser", max_steps=max(10, 3 * len(script)))
    loop = AgentLoop(executor, ScriptedPlanner(script), store=trajectories, agent="workflow-replay", memory=False, traces=traces, sleep=sleep)
    result = loop.run(task)
    healed = heal_workflow(workflow, result.trajectory) if result.ok else []
    for entry in healed:
        executor.events.emit("selector.healed", workflow=workflow.name, task_id=result.trajectory.id, **{k: v for k, v in entry.items() if k not in ("old_selector", "new_selector")})
    saved = False
    if healed and save_heals and workflows is not None:
        workflows.save(workflow)
        saved = True
    return ReplayReport(str(result.status), result.summary, result.trajectory.id, healed, saved)
