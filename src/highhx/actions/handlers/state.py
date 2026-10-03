"""``computer.state``: perception as an action — one fused, immutable ComputerState of a surface.

The agent loop, recorded-workflow replay and grounding all observe through this action, so
capturing the screen is classified (``screen:capture`` when pixels are taken), policy-checked and
audited like everything else. A policy can deny it. Vision is opt-in per call, and a *remote*
vision model raises the risk floor to HIGH, because screenshots then leave this computer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError

if TYPE_CHECKING:
    from highhx.perception.engine import PerceptionEngine
    from highhx.perception.state import ComputerState

KEEP_CAPTURES = 20
"""State screenshots kept on disk (older ones are deleted)."""


def captures_dir() -> Path:
    from highhx.utils.paths import user_data_dir

    return user_data_dir() / "screenshots" / "state"


def _persist(state: ComputerState) -> ComputerState:
    """Write a browser/Android capture (held only in memory) to a file, so the serialized state
    still points at its pixels, and keep the folder bounded."""
    shot = state.screenshot
    if shot is None or shot.path or shot.data is None:
        return state
    folder = captures_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"state-{shot.sha256[:16]}.png"
    path.write_bytes(shot.data)
    files = sorted(folder.glob("state-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_CAPTURES:]:
        old.unlink(missing_ok=True)
    from dataclasses import replace

    return state.with_(screenshot=replace(shot, path=str(path)))


def engine_for(ctx: ActionContext, surface: str, inputs: Inputs) -> PerceptionEngine:
    """The perception engine for ``surface``, from this action's computer session."""
    from highhx.perception.engine import PerceptionEngine
    from highhx.perception.providers import (
        BrowserDOM,
        BrowserScreenshots,
        DesktopAccessibility,
        DesktopScreenshots,
        ModelVisionProvider,
        TesseractOCRProvider,
    )

    session = ctx.computer()
    session.cancel = ctx.cancel
    vision = None
    if str(inputs.get("vision") or "never") != "never":
        from highhx.models.registry import vision_model

        vision = ModelVisionProvider(vision_model(ctx.app, ctx.cancel, remote_ok=bool(inputs.get("remote_vision"))))
    emit = ctx.app.ctx.events.emit
    if surface == "browser":
        browser = session.browser
        return PerceptionEngine(
            "browser",
            structure=[BrowserDOM(browser)],
            screenshot=BrowserScreenshots(browser),
            ocr=TesseractOCRProvider(),
            vision=vision,
            emit=emit,
        )
    if surface == "desktop":
        driver = session.driver()
        return PerceptionEngine(
            "desktop",
            structure=[DesktopAccessibility(driver, app=inputs.get("app"))],
            screenshot=DesktopScreenshots(driver, max_size=inputs.get("max_size")),
            ocr=TesseractOCRProvider(),
            vision=vision,
            emit=emit,
        )
    if surface == "android":
        try:
            from highhx.drivers.android.perception import android_engine
        except ImportError:
            raise ToolError("Android perception is not available in this build") from None
        return android_engine(ctx, inputs, vision=vision, emit=emit)
    raise ToolError(f"unknown surface {surface!r} (expected desktop, browser or android)")


def computer_state(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.perception.engine import PerceptionPolicy

    surface = str(inputs.get("surface") or "browser")
    policy = PerceptionPolicy(
        screenshot=bool(inputs.get("screenshot")),
        ocr=str(inputs.get("ocr") or ("never" if surface == "browser" else "auto")),
        vision=str(inputs.get("vision") or "never"),
        cache_ttl=0.0,
    )
    engine = engine_for(ctx, surface, inputs)
    state = _persist(engine.observe(policy=policy, query=inputs.get("query"), cancel=ctx.cancel))
    from highhx.core.events import current_context

    # every snapshot is traceable to the task, session, step, action and execution that took it
    state = state.with_(
        metadata=tuple(sorted({**dict(state.metadata), **current_context()}.items())),
        cwd=state.cwd or str(ctx.app.root),
    )
    data = state.to_dict()
    limit = int(inputs.get("limit") or 400)
    if len(data["elements"]) > limit:
        data["elements"] = data["elements"][:limit]
        data["truncated"] = True
    failed = [p for p in state.perception if p.status in ("failed", "unavailable") and p.source in ("dom", "ax", "android")]
    if failed and not state.elements and not state.screenshot:
        detail = "; ".join(f"{p.source}: {p.detail}" for p in failed)
        return ActionResult(False, output={"state": data}, error=f"nothing could be observed ({detail})", summary="not observed")
    where = state.url or state.active_app or surface
    return ActionResult(
        True,
        output={"state": data},
        summary=f"{where} · {len(state.elements)} element(s)" + (" · screenshot" if state.screenshot else ""),
        verified=True,
    )


def wants_pixels(inputs: Inputs) -> bool:
    return bool(inputs.get("screenshot")) or str(inputs.get("ocr") or "") == "always" or str(
        inputs.get("vision") or "never"
    ) != "never" or (str(inputs.get("ocr") or "auto") == "auto" and str(inputs.get("surface") or "browser") != "browser")


def state_policy(inputs: Inputs) -> str:
    return "screen:capture" if wants_pixels(inputs) else f"computer:observe:{inputs.get('surface') or 'browser'}"


def state_from_result(result: ActionResult) -> ComputerState | None:
    from highhx.perception.state import ComputerState

    data: Any = result.output.get("state")
    return ComputerState.from_dict(data) if isinstance(data, dict) else None
