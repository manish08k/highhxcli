"""Hybrid grounding: the escalation order, recorded attempts, ambiguity (never a guess), tie
breaking, task weights, on-demand escalation, healing selectors and vision grounding with a
mocked model."""

from __future__ import annotations

import json
from typing import Any

from highhx.agent.model.capabilities import ModelCapabilities
from highhx.grounding import HybridGrounder, Target
from highhx.grounding.selectors import DOMSelector, stable_classes
from highhx.models import ChatLanguageModel, ChatVisionModel, HashingEmbedding
from highhx.models.adapters import cosine
from highhx.perception.providers import ModelVisionProvider
from highhx.perception.state import BrowserState, ComputerState, ScreenshotRef
from tests.unit.agent.conftest import ScriptedProvider, reply
from tests.unit.perception.fakes import el, png


def page(*elements: Any, **kw: Any) -> ComputerState:
    kw.setdefault("browser", BrowserState("https://app.test/billing", "Billing"))
    return ComputerState("browser", elements=tuple(elements), **kw)


def test_accessibility_first_and_attempts_recorded() -> None:
    events: list[tuple[str, dict]] = []
    grounder = HybridGrounder(emit=lambda n, **d: events.append((n, d)))
    state = page(el("e1", "link", "Invoices", (10, 10, 80, 20)), el("e2", "button", "Pay", (10, 40, 80, 20)))
    result = grounder.ground(state, Target.of("Invoices", "link"))
    assert result.grounded and result.strategy == "accessibility" and result.candidate.element.id == "e1"
    assert [a.result for a in result.attempts] == ["success"]
    assert [n for n, _ in events] == ["grounding.started", "grounding.attempt", "grounding.completed"]


def test_falls_back_through_dom_when_the_label_changed() -> None:
    recorded = page(
        el("e1", "button", "Submit", (100, 100, 80, 30), attrs={"testid": "checkout-submit", "tag": "button"}),
    )
    target = Target.from_element(recorded.elements[0], recorded)
    renamed = page(el("x9", "button", "Place order", (300, 120, 90, 30), attrs={"testid": "checkout-submit", "tag": "button"}))
    result = HybridGrounder().ground(renamed, target)
    assert result.grounded and result.strategy == "dom" and result.candidate.element.id == "x9"
    assert [(a.strategy, a.result) for a in result.attempts] == [("accessibility", "failed"), ("dom", "success")]


def test_text_then_ocr_then_vision() -> None:
    target = Target.of("Export PDF", "button")
    reworded = page(el("e5", "menuitem", "Export as PDF", (0, 0, 10, 10)))
    result = HybridGrounder().ground(reworded, target)
    assert result.strategy in ("accessibility", "text") and result.candidate.element.id == "e5"
    canvas = page(el("o1", "text", "Export PDF", (40, 40, 60, 12), source="ocr", confidence=0.95))
    result = HybridGrounder().ground(canvas, Target.of("Export PDF"))
    assert result.grounded and result.strategy == "ocr" and result.point == (70, 46)


def test_ambiguity_is_reported_never_guessed() -> None:
    state = page(el("e1", "button", "Delete", (0, 0, 50, 20)), el("e2", "button", "Delete", (0, 100, 50, 20)))
    result = HybridGrounder().ground(state, Target.of("Delete", "button"))
    assert result.status == "ambiguous" and not result.grounded and len(result.alternatives) == 2
    assert result.attempts[0].result == "ambiguous"


def test_ties_are_broken_by_other_representations() -> None:
    recorded = page(
        el("e1", "button", "Delete", (0, 0, 50, 20), attrs={"dom_id": "del-1"}),
        el("e2", "button", "Delete", (0, 100, 50, 20), attrs={"dom_id": "del-2"}),
    )
    target = Target.from_element(recorded.elements[1], recorded)
    # a third Delete button appears: the recorded index no longer applies, the DOM id does
    later = page(
        el("a", "button", "Delete", (0, 0, 50, 20), attrs={"dom_id": "del-0"}),
        el("b", "button", "Delete", (0, 50, 50, 20), attrs={"dom_id": "del-1"}),
        el("c", "button", "Delete", (0, 100, 50, 20), attrs={"dom_id": "del-2"}),
    )
    result = HybridGrounder().ground(later, target)
    assert result.grounded and result.candidate.element.id == "c"
    # same count as recorded: the occurrence index is trusted
    same = page(el("p", "button", "Delete", (0, 0, 50, 20)), el("q", "button", "Delete", (0, 100, 50, 20)))
    assert HybridGrounder().ground(same, target).candidate.element.id == "q"


def test_task_weights_and_strategy_selection() -> None:
    state = page(
        el("e1", "button", "Go", (0, 0, 10, 10)),
        el("o1", "text", "Go", (200, 200, 20, 10), source="ocr"),
    )
    only_ocr = HybridGrounder().ground(state, Target.of("Go"), strategies=["ocr"])
    assert only_ocr.strategy == "ocr" and only_ocr.candidate.element.id == "o1"
    distrust = HybridGrounder(weights={"accessibility": 0.2, "text": 0.2}).ground(state, Target.of("Go"))
    assert distrust.strategy == "ocr"


def test_escalation_fetches_a_richer_observation_on_demand() -> None:
    bare = page(el("e1", "button", "Save", (0, 0, 10, 10)))
    richer = page(el("e1", "button", "Save", (0, 0, 10, 10)), el("o1", "text", "Totals", (50, 50, 40, 10), source="ocr"))
    asked: list[tuple[str, str]] = []

    def escalate(level: str, query: str) -> ComputerState:
        asked.append((level, query))
        return richer

    result = HybridGrounder().ground(bare, Target.of("Totals"), escalate=escalate)
    assert asked == [("ocr", "Totals")] and result.strategy == "ocr" and result.state is richer


def test_coordinates_only_in_the_same_context() -> None:
    recorded = page(el("e1", "button", "Go", (100, 100, 20, 20)), viewport=(1280, 800))
    target = Target.from_element(recorded.elements[0], recorded)
    blank = page(viewport=(1280, 800))
    result = HybridGrounder().ground(blank, target)
    # a recorded point alone is not trusted by default (score 0.3 x 0.5 < the minimum) …
    assert not result.grounded and result.attempts[-1].strategy == "coordinate" and "too low" in result.attempts[-1].detail
    resized = page(viewport=(800, 600))
    assert HybridGrounder().ground(resized, target).status == "not_found"
    elsewhere = page(viewport=(1280, 800), browser=BrowserState("https://other.test/", "Other"))
    attempt = HybridGrounder(min_score=0.1).ground(elsewhere, target).attempts[-1]
    assert attempt.strategy == "coordinate" and attempt.result == "failed" and "recorded on" in attempt.detail
    trusted = HybridGrounder(min_score=0.1).ground(blank, target)  # … only when the task allows it
    assert trusted.grounded and trusted.strategy == "coordinate" and trusted.point == (110, 110)


def test_selectors_round_trip_and_heal() -> None:
    state = page(
        el("e1", "link", "Invoices", (5, 5, 50, 10), attrs={"href": "/invoices", "class": "nav-link css-1x2y3z", "tag": "a"}),
        viewport=(1000, 700),
    )
    target = Target.from_element(state.elements[0], state, description="the invoices section")
    assert target.dom == DOMSelector(tag="a", href="/invoices", classes=("nav-link",))
    assert Target.from_dict(json.loads(json.dumps(target.to_dict()))) == target
    moved = page(el("z", "link", "Billing documents", (400, 5, 90, 10), attrs={"href": "/invoices", "tag": "a"}))
    result = HybridGrounder().ground(moved, target)
    healed = target.healed(result.candidate.element, moved, strategy=result.strategy)
    assert healed.label == "Billing documents" and healed.semantic.description == "the invoices section"
    assert healed.history[-1]["strategy"] == "dom" and "accessibility" in healed.history[-1]["changed"]
    assert stable_classes("btn btn-primary css-9f8e7d sc-AxjAm jsx-123456") == ("btn", "btn-primary")
    parsed = Target.parse('link:"Docs"#2')
    assert parsed.role == "link" and parsed.accessibility.index == 1 and parsed.accessibility.exact


# ------------------------------------------------------------------ vision
CAPS = ModelCapabilities("local", "qwen2.5vl", True, 4, 32000, "json", "pixels", True)


def _vision(*answers: str) -> tuple[ChatVisionModel, ScriptedProvider]:
    provider = ScriptedProvider([reply(a) for a in answers])
    return ChatVisionModel(ChatLanguageModel(provider, CAPS)), provider


def test_vision_grounding_uses_the_states_screenshot_and_scale() -> None:
    model, provider = _vision('{"found": true, "box": [200, 100, 260, 140], "label": "Gear", "role": "icon", "confidence": 0.9}')
    shot = ScreenshotRef.from_bytes(png(400, 300), scale=2.0)
    state = ComputerState("desktop", screenshot=shot)
    result = HybridGrounder(vision=model).ground(state, Target.of("settings gear icon"))
    assert result.grounded and result.strategy == "vision" and result.point == (115, 60)
    sent = provider.requests[0].messages[0].blocks
    assert sent[0].type == "image" and "settings gear icon" in sent[-1].text


def test_vision_answers_are_checked_not_trusted() -> None:
    model, _ = _vision('{"found": true, "box": [9000, 9000, 9100, 9100]}', '{"found": false}', "not json")
    image = png(100, 100)
    assert model.locate(image, "x") == []  # clamped to nothing: outside the image
    assert model.locate(image, "x") == []
    import pytest

    from highhx.core.errors import ModelProviderError

    with pytest.raises(ModelProviderError):
        model.locate(image, "x")


def test_relative_coordinates_and_detection() -> None:
    caps = ModelCapabilities("local", "ui-tars", True, 4, 32000, "json", "relative1000", True)
    provider = ScriptedProvider(
        [reply('{"elements": [{"box": [0, 0, 500, 500], "label": "Panel", "role": "region", "confidence": 0.7}, {"box": [1, 1], "label": "bad"}]}')]
    )
    model = ChatVisionModel(ChatLanguageModel(provider, caps))
    (found,) = model.detect(png(200, 100))
    assert found.box == (0, 0, 100, 50) and found.label == "Panel"


def test_vision_provider_and_unavailable_model() -> None:
    model, _ = _vision('{"found": true, "box": [10, 10, 30, 20], "label": "OK", "confidence": 0.8}')
    provider = ModelVisionProvider(model)
    assert provider.capability().available and "local" in provider.capability().detail
    (element,) = provider.detect(ScreenshotRef.from_bytes(png()), query="OK button")
    assert element.sources == ("vision",) and element.bounds == (10, 10, 20, 10)
    assert not ModelVisionProvider(None).capability().available
    no_model = HybridGrounder().ground(ComputerState("desktop", screenshot=ScreenshotRef.from_bytes(png())), Target.of("Gear"))
    vision = next(a for a in no_model.attempts if a.strategy == "vision")
    assert vision.result == "unavailable" and "no vision model" in vision.detail


def test_a_non_vision_model_is_refused() -> None:
    import pytest

    from highhx.core.errors import ModelProviderError

    caps = ModelCapabilities("local", "text-only", False, 0, 32000, "json", "pixels", True)
    with pytest.raises(ModelProviderError):
        ChatVisionModel(ChatLanguageModel(ScriptedProvider([]), caps))


def test_language_model_reports_usage() -> None:
    from highhx.agent.messages import Usage

    provider = ScriptedProvider([reply("plan: 1. open", usage=Usage(321, 12))])
    out = ChatLanguageModel(provider, CAPS).complete("sys", "plan this")
    assert out.text == "plan: 1. open" and out.input_tokens == 321 and out.output_tokens == 12


def test_hashing_embedding_is_lexical_and_normalised() -> None:
    model = HashingEmbedding(256)
    a, b, c = model.embed(["export the invoices as pdf", "export invoices to pdf", "change the wallpaper"])
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    assert cosine(a, b) > cosine(a, c)


def test_vision_reads_a_serialized_states_capture_file(tmp_path) -> None:
    model, _ = _vision('{"found": true, "box": [10, 10, 30, 30], "label": "Logo", "confidence": 0.9}')
    shot_file = tmp_path / "shot.png"
    shot_file.write_bytes(png())
    shot = ScreenshotRef.from_bytes(png(), path=str(shot_file))
    state = ComputerState.from_dict(ComputerState("browser", screenshot=shot).to_dict())  # as computer.state returns it
    assert state.screenshot.data is None
    result = HybridGrounder(vision=model).ground(state, Target.of("logo"))
    assert result.grounded and result.point == (20, 20)


def test_model_registry_keeps_the_free_pro_and_consent_rules(monkeypatch, agent_project, make_app) -> None:
    import pytest

    from highhx.core.errors import PlanRequiredError
    from highhx.models.registry import language_model, vision_model

    app = make_app(agent_project)
    monkeypatch.setenv("HIGHHX_PLANNER_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("HIGHHX_PLANNER_MODEL", "big")
    with pytest.raises(PlanRequiredError):
        language_model(app)  # a remote endpoint needs consent
    assert language_model(app, remote_ok=True).name.endswith("big")
    monkeypatch.setenv("HIGHHX_PLANNER_BASE_URL", "http://127.0.0.1:11434/v1")
    assert language_model(app).local  # a local model needs neither Pro nor consent
    for name in ("HIGHHX_PLANNER_BASE_URL", "HIGHHX_PLANNER_MODEL", "HIGHHX_VISION_BASE_URL", "HIGHHX_VISION_MODEL", "HIGHHX_VISION_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PlanRequiredError):
        language_model(app, remote_ok=True)  # no local model and not signed in: HighhX Pro is required
    with pytest.raises(PlanRequiredError):
        vision_model(app, remote_ok=True)


def test_relative_position_grounds_unlabeled_controls() -> None:
    from highhx.grounding import RelativeSelector

    state = page(
        el("l1", "text", "Email", (10, 100, 60, 20)),
        el("f1", "textbox", "", (90, 98, 200, 24)),
        el("l2", "text", "Phone", (10, 140, 60, 20)),
        el("f2", "textbox", "", (90, 138, 200, 24)),
        el("b1", "button", "", (300, 98, 24, 24)),
    )
    email_field = Target("", "textbox", relative=RelativeSelector("Email", "right", "textbox"))
    result = HybridGrounder().ground(state, email_field)
    assert result.grounded and result.strategy == "relative" and result.candidate.element.id == "f1"
    below = HybridGrounder().ground(state, Target("", "textbox", relative=RelativeSelector("Email", "below", "textbox")))
    assert below.candidate.element.id == "f2"
    nothing = HybridGrounder().ground(state, Target("", "checkbox", relative=RelativeSelector("Email", "right", "checkbox")))
    assert not nothing.grounded and any(a.strategy == "relative" and "nothing right" in a.detail for a in nothing.attempts)
    assert Target.from_dict(email_field.to_dict()) == email_field
