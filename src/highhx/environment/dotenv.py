"""A careful ``.env`` parser and in-place editor (no external dependency)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from highhx.utils.filesystem import atomic_write_text, read_text
from highhx.utils.platform import supports_posix_permissions
from highhx.utils.validation import ENV_NAME_RE

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$")
_EXPAND_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


@dataclass
class DotEnvEntry:
    key: str
    value: str
    line: int


class DotEnvError(ValueError):
    """Malformed .env content."""


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in _ESCAPES:
            out.append(_ESCAPES[text[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _expand(value: str, known: Mapping[str, str], fallback: Mapping[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in known:
            return known[name]
        if name in fallback:
            return fallback[name]
        return default or ""

    return _EXPAND_RE.sub(_sub, value)


def parse_dotenv(text: str, *, expand: bool = True, environ: Mapping[str, str] | None = None) -> list[DotEnvEntry]:
    """Parse .env text into entries (later duplicates win when converted to a dict)."""
    entries: list[DotEnvEntry] = []
    values: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        lineno = index + 1
        index += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(raw)
        if not match:
            raise DotEnvError(f"line {lineno}: expected KEY=value")
        key, rest = match.group(1), match.group(2)
        if rest.startswith('"'):
            body = rest[1:]
            collected = [body]
            while not _closes(collected[-1]):
                if index >= len(lines):
                    raise DotEnvError(f"line {lineno}: unterminated double-quoted value")
                collected.append(lines[index])
                index += 1
            joined = "\n".join(collected)
            end = _closing_index(joined)
            value = _unescape(joined[:end])
            if expand:
                value = _expand(value, values, environ or {})
        elif rest.startswith("'"):
            end = rest.find("'", 1)
            if end == -1:
                raise DotEnvError(f"line {lineno}: unterminated single-quoted value")
            value = rest[1:end]
        else:
            value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
            if expand:
                value = _expand(value, values, environ or {})
        values[key] = value
        entries.append(DotEnvEntry(key, value, lineno))
    return entries


def _closing_index(text: str) -> int:
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index
        index += 1
    return -1


def _closes(text: str) -> bool:
    return _closing_index(text) != -1


def load_dotenv(path: Path, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load a .env file into a dict (missing file → empty dict)."""
    if not path.is_file():
        return {}
    return {e.key: e.value for e in parse_dotenv(read_text(path), environ=environ)}


def format_value(value: str) -> str:
    """Quote a value for writing to a .env file when needed."""
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+,=%-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("$", "\\$")
    return f'"{escaped}"'


def set_dotenv_value(path: Path, key: str, value: str) -> bool:
    """Set ``key`` in a .env file in place, preserving other lines.

    Returns True if the key already existed. New files get mode 0600 on POSIX.
    """
    if not ENV_NAME_RE.match(key):
        raise DotEnvError(f"invalid variable name: {key!r}")
    existed = False
    lines = read_text(path).splitlines() if path.is_file() else []
    new_line = f"{key}={format_value(value)}"
    output: list[str] = []
    for line in lines:
        match = _LINE_RE.match(line)
        if match and match.group(1) == key and not line.lstrip().startswith("#"):
            if not existed:
                output.append(new_line)
            existed = True
            continue
        output.append(line)
    if not existed:
        output.append(new_line)
    mode = 0o600 if supports_posix_permissions() and not path.exists() else None
    atomic_write_text(path, "\n".join(output) + "\n", mode=mode)
    return existed


def unset_dotenv_value(path: Path, key: str) -> bool:
    """Remove ``key`` from a .env file. Returns True if it was present."""
    if not path.is_file():
        return False
    lines = read_text(path).splitlines()
    kept = [line for line in lines if not ((m := _LINE_RE.match(line)) and m.group(1) == key)]
    if len(kept) == len(lines):
        return False
    atomic_write_text(path, "\n".join(kept) + ("\n" if kept else ""))
    return True
