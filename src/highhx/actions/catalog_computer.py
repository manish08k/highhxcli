"""Catalog entries of the computer-use runtime: perception, the extra browser verbs, HTTP requests,
Android and sandboxes. They are ordinary :class:`~highhx.actions.spec.ActionSpec` s in the one
catalog, run by the one executor."""

from __future__ import annotations

from highhx.actions.handlers import api, state
from highhx.actions.handlers.native import _flow_step
from highhx.actions.policy import Risk
from highhx.actions.spec import BROWSER, DESKTOP, NETWORK, ActionContext, ActionResult, ActionSpec, Inputs
from highhx.cloud.plans import AGENT_COMPUTER_USE
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Any_, Bool, Int, Map, Num, Obj, Prop, Str

LEVEL = Str(choices=("never", "auto", "always"))


def _short(value: str) -> str | None:
    return "at most 200 characters" if len(value) > 200 else None


def _state_risk(inputs: Inputs) -> Risk:
    if str(inputs.get("vision") or "never") != "never" and inputs.get("remote_vision"):
        return Risk.HIGH  # screenshots go to a remote model
    return Risk.LOW


def browser_select(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"select": {"in": str(inputs["target"]), "option": str(inputs["option"])}}, float(inputs.get("timeout") or 10))


def browser_scroll(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return _flow_step(ctx, {"scroll": str(inputs.get("direction") or "down")})


def computer_use_specs() -> list[ActionSpec]:
    specs = [
        ActionSpec(
            "computer.state",
            "Observe a surface (desktop, browser or android) as one fused state: structure (DOM / accessibility), "
            "and — only when asked or needed — a screenshot, OCR and a vision model. Read-only.",
            state.computer_state,
            Obj(
                {
                    "surface": Prop(Str(choices=("desktop", "browser", "android"))),
                    "screenshot": Prop(Bool()),
                    "ocr": Prop(LEVEL),
                    "vision": Prop(LEVEL),
                    "remote_vision": Prop(Bool(), description="Allow a remote vision model (screenshots leave this computer)."),
                    "query": Prop(Str(min_length=1, check=_short), description="What to look for (escalates perception when not found)."),
                    "app": Prop(Str(min_length=1)),
                    "device": Prop(Str(min_length=1), description="Android: the device serial."),
                    "max_size": Prop(Int(minimum=200, maximum=8000)),
                    "limit": Prop(Int(minimum=1, maximum=2000)),
                }
            ),
            {"state": "the ComputerState (elements, text, screenshot reference, what each source did)"},
            Risk.LOW,
            ActionKind.READ,
            (DESKTOP, BROWSER),
            idempotent=True,
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("surface") or "browser"),
            policy_action=state.state_policy,
            risk_for=_state_risk,
        ),
        ActionSpec(
            "browser.select",
            'Choose an option in a list by role and name, e.g. target "combobox:Size", option "L".',
            browser_select,
            Obj(
                {
                    "target": Prop(Str(min_length=1), required=True),
                    "option": Prop(Str(), required=True),
                    "timeout": Prop(Num(minimum=0)),
                }
            ),
            {"step": "outcome"},
            Risk.LOW,
            ActionKind.UI_SELECT,
            (BROWSER,),
            timeout=120,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("target", "")),
        ),
        ActionSpec(
            "browser.scroll",
            "Scroll the page up or down.",
            browser_scroll,
            Obj({"direction": Prop(Str(choices=("up", "down")))}),
            {"step": "outcome"},
            Risk.SAFE,
            ActionKind.UI_SCROLL,
            (BROWSER,),
            timeout=60,
            feature=AGENT_COMPUTER_USE,
            agent=False,
        ),
        ActionSpec(
            "api.request",
            "Make one HTTP(S) request. Reads of this computer are low risk, reads of other hosts medium; changes "
            "(POST/PUT/PATCH/DELETE) and credentials (headers_from_env) are high and always asked.",
            api.request,
            Obj(
                {
                    "url": Prop(Str(min_length=1), required=True),
                    "method": Prop(Str(choices=("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"))),
                    "headers": Prop(Map(Str())),
                    "headers_from_env": Prop(Map(Str(min_length=1)), description="Header → environment variable (never logged)."),
                    "json": Prop(Any_(), description="A JSON body."),
                    "body": Prop(Str()),
                    "timeout": Prop(Num(minimum=1)),
                    "expect_status": Prop(Int(minimum=100, maximum=599)),
                }
            ),
            {"status": "HTTP status", "body": "response text (truncated)", "json": "parsed JSON when applicable"},
            Risk.LOW,
            ActionKind.READ,
            (NETWORK,),
            timeout=300,
            feature=AGENT_COMPUTER_USE,
            agent=False,
            target=lambda i: str(i.get("url", "")),
            policy_action=api.policy_name,
            risk_for=api.risk_for,
        ),
    ]
    return specs
