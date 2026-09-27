"""Entity extraction: URLs, queries, file names, interpreters, key combinations — fixed rules only."""

from __future__ import annotations

import re

from highhx.automation.engine.protocol import KEY_CODES, MODIFIERS

URL = re.compile(
    r"^(?:https?://\S+|(?:localhost|127\.0\.0\.1)(?:[\s:]+\d{2,5})?(?:/\S*)?|[\w-]+(?:\.[\w-]+)+(?:/\S*)?)$", re.I
)
MEDIA_WORDS = re.compile(r"\s+(?:song|songs|video|videos|music|track|album|playlist|clip)$", re.I)
GENERIC_BROWSER = re.compile(r"(?:web\s+)?browser", re.I)
DESCRIPTION = re.compile(r"(?:the|a|an|my|our|this|that|some)\s+\S|(?:\S+\s+){3,}\S", re.I)
"""A target described rather than named — not a name to look up in the registry."""

INTERPRETERS = {
    "python": "python3",
    "python3": "python3",
    "node": "node",
    "ruby": "ruby",
    "bash": "bash",
    "sh": "sh",
    "deno": "deno run",
}
BY_SUFFIX = {".py": "python3", ".js": "node", ".mjs": "node", ".rb": "ruby", ".sh": "bash"}


def as_url(text: str) -> str | None:
    """``text`` as a URL when it is one ("github.com", "localhost 3000"); names and files are not."""
    from highhx.actions.resolver import _url  # the resolver's URL rules (files are never URLs)

    return _url(text.strip()) if URL.match(text.strip()) else None


def query_of(text: str) -> str:
    """A search query or typed text, without surrounding quotes."""
    text = text.strip()
    quoted = re.fullmatch(r"[\"'“‘](.+)[\"'”’]", text)  # noqa: RUF001 - typographic quotes are quotes
    return (quoted.group(1) if quoted else text).strip()


def media_query(text: str) -> str:
    """ "adhento gani song" → "adhento gani"."""
    return query_of(MEDIA_WORDS.sub("", text.strip()))


def split_in(target: str) -> tuple[str, str | None]:
    """``"youtube in safari"`` → ("youtube", "safari")."""
    match = re.fullmatch(r"(.+?)\s+(?:in|with|using)\s+(?:the\s+)?(.+)", target.strip(), re.I)
    return (match.group(1), match.group(2)) if match else (target.strip(), None)


def looks_like_path(text: str) -> bool:
    name = text.strip().split("/")[-1]
    return "/" in text or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", name))


def key_combo(text: str) -> tuple[list[str], str] | None:
    """ "cmd+t" / "command shift t" → (["cmd", "shift"], "t"); None when not a known combination."""
    keys = re.sub(r"\s*(?:\+|-|\s)\s*", "+", text.strip().lower()).split("+")
    keys = [k for k in keys if k]
    if not keys:
        return None
    modifiers, key = keys[:-1], keys[-1]
    if any(m not in MODIFIERS for m in modifiers):
        return None
    if key not in KEY_CODES and len(key) != 1:
        return None
    return modifiers, key
