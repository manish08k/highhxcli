"""A small, safe expression language for ``if:`` conditions and ``${{ }}`` interpolation.

Grammar (no Python ``eval``)::

    expr    := or
    or      := and (("||" | "or") and)*
    and     := not (("&&" | "and") not)*
    not     := ("!" | "not") not | compare
    compare := primary (("==" | "!=" | "<" | "<=" | ">" | ">=") primary)?
    primary := literal | path | call | "(" expr ")"
    path    := ident ("." ident | "[" string "]")*
    call    := ident "(" [expr ("," expr)*] ")"

Status functions: ``success()``, ``failure()``, ``always()``, ``cancelled()``.
Other functions: ``contains(a, b)``, ``startsWith(a, b)``, ``endsWith(a, b)``,
``format(fmt, args...)``, ``toJSON(value)``, ``exists(path)``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUS_FUNCTIONS = frozenset({"success", "failure", "always", "cancelled"})
KNOWN_ROOTS = frozenset({"env", "vars", "inputs", "steps", "workflow", "execution", "platform", "project"})

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>!().,\[\]])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
    """,
    re.VERBOSE,
)


class ExpressionError(ValueError):
    """Invalid expression syntax or evaluation failure."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ExpressionError(f"unexpected character {text[pos]!r} at position {pos + 1}")
        kind = match.lastgroup or ""
        if kind != "ws":
            tokens.append(Token(kind, match.group(), pos))
        pos = match.end()
    return tokens


# ---------------------------------------------------------------- AST nodes
@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class PathRef:
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Any


@dataclass(frozen=True)
class Binary:
    op: str
    left: Any
    right: Any


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        self.index += 1
        return token

    def accept(self, *values: str) -> Token | None:
        token = self.peek()
        if token is not None and token.value in values and token.kind in ("op", "ident"):
            self.index += 1
            return token
        return None

    def expect(self, value: str) -> None:
        token = self.next()
        if token.value != value:
            raise ExpressionError(f"expected '{value}' at position {token.pos + 1}, found '{token.value}'")

    def parse(self) -> Any:
        if not self.tokens:
            raise ExpressionError("empty expression")
        node = self.parse_or()
        if self.peek() is not None:
            token = self.peek()
            assert token is not None
            raise ExpressionError(f"unexpected '{token.value}' at position {token.pos + 1}")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while self.accept("||", "or"):
            node = Binary("or", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_not()
        while self.accept("&&", "and"):
            node = Binary("and", node, self.parse_not())
        return node

    def parse_not(self) -> Any:
        if self.accept("!", "not"):
            return Unary("not", self.parse_not())
        return self.parse_compare()

    def parse_compare(self) -> Any:
        node = self.parse_primary()
        token = self.peek()
        if token is not None and token.kind == "op" and token.value in ("==", "!=", "<", "<=", ">", ">="):
            self.index += 1
            node = Binary(token.value, node, self.parse_primary())
        return node

    def parse_primary(self) -> Any:
        token = self.next()
        if token.kind == "number":
            return Literal(float(token.value) if "." in token.value else int(token.value))
        if token.kind == "string":
            return Literal(_unquote(token.value))
        if token.value == "(":
            node = self.parse_or()
            self.expect(")")
            return node
        if token.kind == "ident":
            lowered = token.value.lower()
            if lowered in ("true", "false"):
                return Literal(lowered == "true")
            if lowered in ("null", "none"):
                return Literal(None)
            if self.peek() is not None and self.peek().value == "(":  # type: ignore[union-attr]
                self.index += 1
                args: list[Any] = []
                if not self.accept(")"):
                    args.append(self.parse_or())
                    while self.accept(","):
                        args.append(self.parse_or())
                    self.expect(")")
                return Call(token.value, tuple(args))
            parts = [token.value]
            while True:
                if self.accept("."):
                    part = self.next()
                    if part.kind not in ("ident", "number"):
                        raise ExpressionError(f"expected a name after '.' at position {part.pos + 1}")
                    parts.append(part.value)
                elif self.accept("["):
                    key = self.next()
                    if key.kind not in ("string", "number"):
                        raise ExpressionError(f"expected a string index at position {key.pos + 1}")
                    parts.append(_unquote(key.value) if key.kind == "string" else key.value)
                    self.expect("]")
                else:
                    break
            return PathRef(tuple(parts))
        raise ExpressionError(f"unexpected '{token.value}' at position {token.pos + 1}")


def _unquote(text: str) -> str:
    if text.startswith("'"):
        return text[1:-1].replace("''", "'").replace("\\'", "'")
    return json.loads(text)


def strip_wrapper(expression: str) -> str:
    """Remove an optional ``${{ … }}`` wrapper."""
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        return text[3:-2].strip()
    return text


def parse(expression: str) -> Any:
    """Parse an expression into an AST (raises :class:`ExpressionError`)."""
    return _Parser(strip_wrapper(expression)).parse()


def walk(node: Any) -> list[Any]:
    """All nodes in the tree (pre-order)."""
    out = [node]
    if isinstance(node, Unary):
        out.extend(walk(node.operand))
    elif isinstance(node, Binary):
        out.extend(walk(node.left))
        out.extend(walk(node.right))
    elif isinstance(node, Call):
        for arg in node.args:
            out.extend(walk(arg))
    return out


def references(expression: str) -> list[tuple[str, ...]]:
    """Path references used by an expression."""
    return [n.parts for n in walk(parse(expression)) if isinstance(n, PathRef)]


def uses_status_function(expression: str) -> bool:
    return any(isinstance(n, Call) and n.name in STATUS_FUNCTIONS for n in walk(parse(expression)))


def constant_value(expression: str) -> tuple[bool, Any]:
    """If an expression has no references or calls, return ``(True, value)``."""
    node = parse(expression)
    if any(isinstance(n, PathRef | Call) for n in walk(node)):
        return False, None
    return True, Evaluator(EvalContext()).evaluate(node)


# ---------------------------------------------------------------- evaluation
@dataclass
class EvalContext:
    """Data available to expressions."""

    data: Mapping[str, Any] = field(default_factory=dict)
    status: Mapping[str, bool] = field(default_factory=lambda: {"success": True, "failure": False, "cancelled": False})
    base_dir: Path | None = None
    strict: bool = True
    """Unknown references raise when strict; otherwise they evaluate to None."""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value != "" and value.lower() not in ("false", "0")
    return bool(value)


def _compare_values(op: str, left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        left, right = left.lower(), right.lower()
    elif isinstance(left, str) and isinstance(right, int | float) and not isinstance(right, bool):
        try:
            left = float(left)
        except ValueError:
            return op == "!="
    elif isinstance(right, str) and isinstance(left, int | float) and not isinstance(left, bool):
        try:
            right = float(right)
        except ValueError:
            return op == "!="
    elif isinstance(left, bool) and isinstance(right, str):
        right = right.lower() == "true"
    elif isinstance(right, bool) and isinstance(left, str):
        left = left.lower() == "true"
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    try:
        return bool({"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op])
    except TypeError:
        return False


class Evaluator:
    """Evaluates parsed expressions against an :class:`EvalContext`."""

    def __init__(self, ctx: EvalContext) -> None:
        self.ctx = ctx
        self.functions: dict[str, Callable[..., Any]] = {
            "contains": lambda a, b: (str(b).lower() in str(a).lower()) if not isinstance(a, list | dict) else b in a,
            "startswith": lambda a, b: str(a).lower().startswith(str(b).lower()),
            "endswith": lambda a, b: str(a).lower().endswith(str(b).lower()),
            "format": self._format,
            "tojson": lambda v: json.dumps(v, default=str),
            "exists": self._exists,
        }

    def _format(self, fmt: Any, *args: Any) -> str:
        text = str(fmt)
        for index, arg in enumerate(args):
            text = text.replace("{" + str(index) + "}", "" if arg is None else str(arg))
        return text

    def _exists(self, path: Any) -> bool:
        base = self.ctx.base_dir or Path.cwd()
        return (base / str(path)).exists()

    def evaluate(self, node: Any) -> Any:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, PathRef):
            return self._resolve(node.parts)
        if isinstance(node, Unary):
            return not _truthy(self.evaluate(node.operand))
        if isinstance(node, Binary):
            if node.op == "and":
                left = self.evaluate(node.left)
                return self.evaluate(node.right) if _truthy(left) else left
            if node.op == "or":
                left = self.evaluate(node.left)
                return left if _truthy(left) else self.evaluate(node.right)
            return _compare_values(node.op, self.evaluate(node.left), self.evaluate(node.right))
        if isinstance(node, Call):
            name = node.name.lower()
            if name in STATUS_FUNCTIONS:
                if node.args:
                    raise ExpressionError(f"{node.name}() takes no arguments")
                return True if name == "always" else bool(self.ctx.status.get(name, False))
            func = self.functions.get(name)
            if func is None:
                raise ExpressionError(f"unknown function '{node.name}'")
            try:
                return func(*(self.evaluate(arg) for arg in node.args))
            except TypeError as exc:
                raise ExpressionError(f"bad arguments for {node.name}(): {exc}") from exc
        raise ExpressionError(f"cannot evaluate {node!r}")

    def _resolve(self, parts: tuple[str, ...]) -> Any:
        current: Any = self.ctx.data
        for index, part in enumerate(parts):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                if not self.ctx.strict or (index > 0 and parts[0] in ("env", "vars", "inputs")):
                    return None
                raise ExpressionError(f"unknown reference '{'.'.join(parts)}'")
        return current


def evaluate(expression: str, ctx: EvalContext) -> Any:
    return Evaluator(ctx).evaluate(parse(expression))


def evaluate_condition(expression: str | None, ctx: EvalContext) -> bool:
    """Evaluate an ``if:`` condition.

    Without an explicit status function the condition is implicitly
    ``success() && (<expression>)`` — mirroring common CI semantics.
    """
    if expression is None or not str(expression).strip():
        return bool(ctx.status.get("success", True))
    node = parse(str(expression))
    result = _truthy(Evaluator(ctx).evaluate(node))
    if not any(isinstance(n, Call) and n.name.lower() in STATUS_FUNCTIONS for n in walk(node)):
        return bool(ctx.status.get("success", True)) and result
    return result
