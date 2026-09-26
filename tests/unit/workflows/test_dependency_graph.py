import pytest

from highhx.core.errors import DependencyCycleError
from highhx.workflows.dependency_graph import DependencyGraph


def graph(mapping: dict[str, list[str]]) -> DependencyGraph:
    return DependencyGraph.from_mapping(mapping)


def test_topological_order_and_levels() -> None:
    g = graph({"test": [], "lint": [], "build": ["test", "lint"], "deploy": ["build"]})
    order = g.topological_order()
    assert order.index("build") > order.index("test") and order.index("build") > order.index("lint")
    assert g.levels() == [["test", "lint"], ["build"], ["deploy"]]


def test_cycle_detection_reports_path() -> None:
    g = graph({"a": ["c"], "b": ["a"], "c": ["b"]})
    cycle = g.find_cycle()
    assert cycle is not None and cycle[0] == cycle[-1] and set(cycle) == {"a", "b", "c"}
    with pytest.raises(DependencyCycleError):
        g.topological_order()


def test_self_cycle() -> None:
    assert graph({"a": ["a"]}).find_cycle() == ["a", "a"]


def test_missing_dependencies() -> None:
    assert graph({"a": ["ghost"]}).missing() == [("a", "ghost")]


def test_ancestors_descendants_ready() -> None:
    g = graph({"a": [], "b": ["a"], "c": ["b"], "d": []})
    assert g.ancestors("c") == ["b", "a"]
    assert g.descendants("a") == ["b", "c"]
    assert g.ready(finished=["a"], started=["a", "d"]) == ["b"]


def test_renderers() -> None:
    g = graph({"a": [], "b": ["a"]})
    assert "Stage 2" in g.render_text()
    assert '"a" -> "b"' in g.to_dot()
    assert "a --> b" in g.to_mermaid()
