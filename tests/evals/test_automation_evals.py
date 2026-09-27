"""Deterministic evals for the Free resolver (``tests/evals/automation.yaml``).

Every case must pass — the resolver is deterministic, so an eval is a regression test with a score.
The summary test reports accuracy per dimension (route, intent, target, risk, executor,
actions, parameters, verification, clause splitting) so a failing change shows *what kind*
of understanding broke."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from highhx.actions.resolver import ResolverContext
from highhx.decision.deterministic import Decision, DeterministicDecider
from highhx.language.targets import default_registry

CASES: list[dict[str, Any]] = yaml.safe_load((Path(__file__).parent / "automation.yaml").read_text())["cases"]


@pytest.fixture(scope="module")
def decider(tmp_path_factory: pytest.TempPathFactory) -> DeterministicDecider:
    root = tmp_path_factory.mktemp("evalproject")
    (root / "hello.py").write_text("print('hello')\n")
    (root / "README.md").write_text("# demo\n")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    return DeterministicDecider(ResolverContext(root=root, _targets=default_registry()))


def check(case: dict[str, Any], decision: Decision) -> dict[str, bool]:
    """Each checked dimension → pass/fail."""
    plan = decision.plan
    steps = plan.steps if plan else ()
    results: dict[str, bool] = {"route": decision.route == case["route"]}
    if "intent" in case:
        results["intent"] = bool(plan) and plan.intent == case["intent"]  # type: ignore[union-attr]
    if "target" in case:
        results["target"] = bool(plan) and plan.target == case["target"]  # type: ignore[union-attr]
    if "risk" in case:
        results["risk"] = bool(plan) and plan.risk == case["risk"]  # type: ignore[union-attr]
    if "executor" in case:
        results["executor"] = bool(plan) and plan.executor == case["executor"]  # type: ignore[union-attr]
    if "verify" in case:
        results["verification"] = bool(plan) and plan.verification == case["verify"]  # type: ignore[union-attr]
    if "actions" in case:
        results["actions"] = [s.catalog_action for s in steps] == case["actions"]
    if "primitives" in case:
        results["actions"] = [s.action for s in steps] == case["primitives"]
    if "params" in case:
        results["params"] = all(
            len(steps) >= int(index) and all(steps[int(index) - 1].params.get(k) == v for k, v in expected.items())
            for index, expected in case["params"].items()
        )
    if "clauses" in case:
        results["clauses"] = list(decision.clauses) == case["clauses"]
    if "reason" in case:
        reason = decision.unknown.reason if decision.unknown else decision.reason
        results["reason"] = case["reason"] in reason
    return results


@pytest.mark.parametrize("case", CASES, ids=[c["request"] for c in CASES])
def test_eval_case(decider: DeterministicDecider, case: dict[str, Any]) -> None:
    decision = decider.decide(case["request"])
    results = check(case, decision)
    failed = [name for name, ok in results.items() if not ok]
    assert not failed, f"{case['request']!r}: {failed} — got {decision.to_dict()}"


def test_eval_summary(decider: DeterministicDecider, capsys: pytest.CaptureFixture[str]) -> None:
    passed: Counter[str] = Counter()
    total: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    for case in CASES:
        routes[case["route"]] += 1
        for name, ok in check(case, decider.decide(case["request"])).items():
            total[name] += 1
            passed[name] += ok
    with capsys.disabled():
        print(f"\nDeterministic evals: {len(CASES)} cases ({dict(routes)})")
        for name in sorted(total):
            print(f"  {name:<13} {passed[name]}/{total[name]}")
    assert passed == total
    assert routes["local"] >= 40 and routes["pro"] >= 6 and routes["unknown"] >= 8
