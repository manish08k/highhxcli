"""Directed acyclic dependency graphs for workflow steps and tasks."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping

from highhx.core.errors import DependencyCycleError


class DependencyGraph:
    """Nodes with ``depends_on`` edges. Insertion order is preserved everywhere
    so that execution order and output are deterministic."""

    def __init__(self) -> None:
        self._deps: dict[str, list[str]] = {}

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Iterable[str]]) -> DependencyGraph:
        graph = cls()
        for node in mapping:
            graph.add_node(node)
        for node, deps in mapping.items():
            for dep in deps:
                graph.add_edge(node, dep)
        return graph

    # ------------------------------------------------------------- building
    def add_node(self, node: str) -> None:
        self._deps.setdefault(node, [])

    def add_edge(self, node: str, depends_on: str) -> None:
        """``node`` depends on ``depends_on``."""
        self.add_node(node)
        if depends_on not in self._deps[node]:
            self._deps[node].append(depends_on)

    # -------------------------------------------------------------- queries
    @property
    def nodes(self) -> list[str]:
        return list(self._deps)

    def __contains__(self, node: object) -> bool:
        return node in self._deps

    def __len__(self) -> int:
        return len(self._deps)

    def dependencies(self, node: str) -> list[str]:
        return list(self._deps.get(node, []))

    def dependents(self, node: str) -> list[str]:
        return [n for n, deps in self._deps.items() if node in deps]

    def missing(self) -> list[tuple[str, str]]:
        """``(node, dependency)`` pairs whose dependency is not a node."""
        return [(n, d) for n, deps in self._deps.items() for d in deps if d not in self._deps]

    def ancestors(self, node: str) -> list[str]:
        """All transitive dependencies of ``node`` (deterministic order)."""
        seen: list[str] = []
        stack = list(reversed(self._deps.get(node, [])))
        while stack:
            current = stack.pop()
            if current in seen or current not in self._deps:
                continue
            seen.append(current)
            stack.extend(reversed(self._deps[current]))
        return seen

    def descendants(self, node: str) -> list[str]:
        seen: list[str] = []
        queue = deque(self.dependents(node))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.append(current)
            queue.extend(self.dependents(current))
        return seen

    def find_cycle(self) -> list[str] | None:
        """Return a cycle as ``[a, b, …, a]`` or None. Missing nodes are ignored."""
        white, grey, black = 0, 1, 2
        color = dict.fromkeys(self._deps, white)
        parent: dict[str, str] = {}

        for start in self._deps:
            if color[start] != white:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = grey
            while stack:
                node, index = stack[-1]
                deps = [d for d in self._deps[node] if d in self._deps]
                if index < len(deps):
                    stack[-1] = (node, index + 1)
                    nxt = deps[index]
                    if color[nxt] == white:
                        color[nxt] = grey
                        parent[nxt] = node
                        stack.append((nxt, 0))
                    elif color[nxt] == grey:
                        cycle = [nxt]
                        cur = node
                        while cur != nxt:
                            cycle.append(cur)
                            cur = parent[cur]
                        cycle.append(nxt)
                        cycle.reverse()
                        return cycle
                else:
                    color[node] = black
                    stack.pop()
        return None

    def topological_order(self) -> list[str]:
        """Dependencies before dependents; raises :class:`DependencyCycleError`."""
        cycle = self.find_cycle()
        if cycle:
            raise DependencyCycleError(cycle)
        order: list[str] = []
        done: set[str] = set()
        remaining = list(self._deps)
        while remaining:
            progressed = False
            for node in list(remaining):
                if all(d in done or d not in self._deps for d in self._deps[node]):
                    order.append(node)
                    done.add(node)
                    remaining.remove(node)
                    progressed = True
            if not progressed:  # pragma: no cover - guarded by find_cycle
                raise DependencyCycleError(remaining)
        return order

    def levels(self) -> list[list[str]]:
        """Group nodes into batches that can run in parallel."""
        cycle = self.find_cycle()
        if cycle:
            raise DependencyCycleError(cycle)
        level: dict[str, int] = {}
        for node in self.topological_order():
            deps = [d for d in self._deps[node] if d in self._deps]
            level[node] = 1 + max((level[d] for d in deps), default=-1)
        batches: list[list[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
        for node in self._deps:
            batches[level[node]].append(node)
        return batches

    def ready(self, finished: Iterable[str], started: Iterable[str]) -> list[str]:
        """Nodes whose dependencies are all finished and which have not started."""
        done = set(finished)
        begun = set(started)
        return [
            n
            for n, deps in self._deps.items()
            if n not in begun and n not in done and all(d in done for d in deps if d in self._deps)
        ]

    # ------------------------------------------------------------ rendering
    def render_text(self, labels: Mapping[str, str] | None = None) -> str:
        """Human-readable tree of execution stages."""
        labels = labels or {}
        lines: list[str] = []
        for index, batch in enumerate(self.levels(), start=1):
            parallel = " (parallel)" if len(batch) > 1 else ""
            lines.append(f"Stage {index}{parallel}")
            for position, node in enumerate(batch):
                branch = "└─" if position == len(batch) - 1 else "├─"
                deps = self._deps[node]
                after = f"  ← {', '.join(deps)}" if deps else ""
                label = f" {labels[node]}" if labels.get(node) else ""
                lines.append(f"  {branch} {node}{label}{after}")
        return "\n".join(lines)

    def to_dot(self, name: str = "workflow") -> str:
        lines = [f'digraph "{name}" {{', "  rankdir=LR;"]
        for node in self._deps:
            lines.append(f'  "{node}";')
        for node, deps in self._deps.items():
            for dep in deps:
                lines.append(f'  "{dep}" -> "{node}";')
        lines.append("}")
        return "\n".join(lines)

    def to_mermaid(self) -> str:
        lines = ["graph LR"]
        for node, deps in self._deps.items():
            if not deps:
                lines.append(f"  {node}")
            for dep in deps:
                lines.append(f"  {dep} --> {node}")
        return "\n".join(lines)
