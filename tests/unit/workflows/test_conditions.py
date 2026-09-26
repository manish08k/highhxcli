import pytest

from highhx.workflows.conditions import (
    EvalContext,
    ExpressionError,
    constant_value,
    evaluate,
    evaluate_condition,
    references,
)
from highhx.workflows.variables import interpolate, try_interpolate


def ctx(**status: bool) -> EvalContext:
    data = {
        "env": {"CI": "true", "COUNT": "3"},
        "vars": {"name": "app", "items": ["x", "y"]},
        "inputs": {"target": "staging"},
        "steps": {"build": {"status": "success", "outputs": {"version": "1.2.0"}, "exit_code": 0}},
        "platform": "linux",
    }
    return EvalContext(data=data, status={"success": True, "failure": False, "cancelled": False, **status})


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("env.CI == 'true'", True),
        ("env.CI == true", True),
        ("env.COUNT > 2", True),
        ("inputs.target != 'production' && vars.name == 'app'", True),
        ("!(env.CI == 'false')", True),
        ("steps.build.outputs.version == '1.2.0'", True),
        ("contains(vars.items, 'y')", True),
        ("startsWith(inputs.target, 'stag')", True),
        ("env.MISSING == ''", False),
        ("platform == 'windows' or platform == 'linux'", True),
    ],
)
def test_expressions(expr: str, expected: bool) -> None:
    assert bool(evaluate(expr, ctx())) is expected


def test_implicit_success_guard() -> None:
    assert evaluate_condition("env.CI == 'true'", ctx(success=False)) is False
    assert evaluate_condition("always()", ctx(success=False)) is True
    assert evaluate_condition("failure()", ctx(success=False, failure=True)) is True
    assert evaluate_condition(None, ctx()) is True


def test_syntax_errors() -> None:
    for bad in ("env.CI ==", "(a", "foo(", "a $ b", ""):
        with pytest.raises(ExpressionError):
            evaluate(bad, ctx())


def test_unknown_reference_is_strict() -> None:
    with pytest.raises(ExpressionError):
        evaluate("steps.nope.status", ctx())


def test_references_and_constants() -> None:
    assert ("steps", "build", "outputs", "version") in references("${{ steps.build.outputs.version }}")
    assert constant_value("false") == (True, False)
    assert constant_value("env.CI")[0] is False


def test_interpolation() -> None:
    assert (
        interpolate("deploy ${{ inputs.target }} v${{ steps.build.outputs.version }}", ctx()) == "deploy staging v1.2.0"
    )
    assert try_interpolate("x ${{ steps.later.outputs.v }}", ctx()) == "x ${{ steps.later.outputs.v }}"
