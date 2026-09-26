"""Secret detection patterns and output redaction."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

REDACTED = "[REDACTED]"

SECRET_KEY_HINTS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "PWD",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "AUTH",
    "DSN",
    "CONNECTION_STRING",
    "DATABASE_URL",
    "SESSION_KEY",
    "SIGNING_KEY",
    "CLIENT_SECRET",
    "WEBHOOK",
)
NON_SECRET_KEY_HINTS = ("PUBLIC", "_PATH", "_FILE", "_URL_PUBLIC", "AUTHOR", "_SOCK", "_DIR")
NON_SECRET_KEYS = frozenset({"PWD", "OLDPWD", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "XAUTHORITY", "HIGHHX_OUTPUT"})

PLACEHOLDER_RE = re.compile(
    r"^(|x+|\*+|changeme|change_me|example|placeholder|your[_-].*|<.*>|\$\{.*\}|\$[A-Z_]+|"
    r"todo|none|null|dummy|test|secret|password|pass|pwd|passwd|wrong|user|redacted|\[redacted\]|\.\.\.|\{.*\}|%s)$",
    re.IGNORECASE,
)


def is_secret_key(name: str) -> bool:
    """Heuristic: does an environment variable / config key hold a secret?"""
    upper = name.upper()
    if upper in NON_SECRET_KEYS or any(hint in upper for hint in NON_SECRET_KEY_HINTS):
        return False
    return any(hint in upper for hint in SECRET_KEY_HINTS)


def is_placeholder(value: str) -> bool:
    """True for obvious placeholder values such as ``changeme`` or ``${VAR}``."""
    return bool(PLACEHOLDER_RE.match(value.strip().strip("'\"")))


def shannon_entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def mask(value: str | None) -> str:
    """Mask a secret value entirely; only its presence and length are revealed."""
    if value is None or value == "":
        return "(empty)"
    return f"******** ({len(value)} chars)"


@dataclass(frozen=True)
class SecretPattern:
    """A named regex for a class of secret. Group ``secret`` (or 0) is the sensitive part."""

    id: str
    description: str
    regex: re.Pattern[str]
    severity: str = "high"
    min_entropy: float = 0.0


def _p(pid: str, description: str, pattern: str, severity: str = "high", min_entropy: float = 0.0) -> SecretPattern:
    return SecretPattern(pid, description, re.compile(pattern), severity, min_entropy)


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    _p(
        "private-key",
        "Private key block",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----",
        "critical",
    ),
    _p(
        "aws-access-key-id",
        "AWS access key id",
        r"\b(?P<secret>(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16})\b",
        "high",
    ),
    _p(
        "aws-secret-access-key",
        "AWS secret access key",
        r"(?i)aws_?secret_?access_?key\s*[:=]\s*['\"]?(?P<secret>[A-Za-z0-9/+=]{40})\b",
        "critical",
    ),
    _p(
        "github-token",
        "GitHub token",
        r"\b(?P<secret>(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b",
        "critical",
    ),
    _p("gitlab-token", "GitLab personal access token", r"\b(?P<secret>glpat-[A-Za-z0-9_\-]{20,})\b", "critical"),
    _p("slack-token", "Slack token", r"\b(?P<secret>xox[abposr]-[A-Za-z0-9-]{10,})\b", "high"),
    _p(
        "slack-webhook",
        "Slack webhook URL",
        r"(?P<secret>https://hooks\.slack\.com/services/T[A-Za-z0-9_]+/B[A-Za-z0-9_]+/[A-Za-z0-9_]+)",
        "high",
    ),
    _p("stripe-key", "Stripe secret key", r"\b(?P<secret>(?:sk|rk)_live_[A-Za-z0-9]{20,})\b", "critical"),
    _p("google-api-key", "Google API key", r"\b(?P<secret>AIza[0-9A-Za-z\-_]{35})\b", "high"),
    _p("npm-token", "npm access token", r"\b(?P<secret>npm_[A-Za-z0-9]{36})\b", "critical"),
    _p("pypi-token", "PyPI upload token", r"\b(?P<secret>pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,})\b", "critical"),
    _p(
        "jwt",
        "JSON Web Token",
        r"\b(?P<secret>eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b",
        "medium",
    ),
    _p(
        "url-credentials",
        "Credentials embedded in a URL",
        r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:(?P<secret>[^\s@/'\"]{3,})@[^\s/'\"]+",
        "high",
    ),
    _p(
        "generic-secret-assignment",
        "Hard-coded secret assigned to a sensitive name",
        r"(?i)\b[\w.-]*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)[\w.-]*"
        r"\s*[:=]\s*(?:['\"](?P<secret>[^'\"\s]{8,})['\"]|(?P<bare>[^\s'\"#,;]{8,}))",
        "medium",
        3.0,
    ),
)


@dataclass(frozen=True)
class SecretMatch:
    """One detected secret (the value itself is never stored)."""

    pattern_id: str
    description: str
    severity: str
    line: int
    column: int
    length: int


_CODE_CHARS = frozenset("()[]{}")


def _plausible_bare_value(value: str) -> bool:
    """An unquoted assignment value looks like a credential (not code or a word)."""
    if any(ch in _CODE_CHARS for ch in value):
        return False
    return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)


def find_secrets(
    text: str, patterns: Iterable[SecretPattern] = SECRET_PATTERNS, *, source_code: bool = False
) -> list[SecretMatch]:
    """Find likely secrets in ``text``. Placeholders and low-entropy values are ignored.

    With ``source_code=True`` (Python, JavaScript, Go … files) a generic
    ``name = value`` assignment only counts when the value is a quoted string
    literal — ``token = self.next()`` is code, not a credential.
    """
    matches: list[SecretMatch] = []
    pats = list(patterns)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            line = line[:4000]
        if "highhx:allow-secret" in line or "pragma: allowlist secret" in line:
            continue
        seen_spans: list[tuple[int, int]] = []
        for pattern in pats:
            for m in pattern.regex.finditer(line):
                group: str | int = 0
                if "secret" in pattern.regex.groupindex:
                    group = (
                        "secret" if m.group("secret") is not None or "bare" not in pattern.regex.groupindex else "bare"
                    )
                value = m.group(group) or ""
                start, end = m.span(group)
                if group == "bare" and (source_code or not _plausible_bare_value(value)):
                    continue
                if pattern.id != "private-key":
                    if is_placeholder(value):
                        continue
                    if pattern.min_entropy and shannon_entropy(value) < pattern.min_entropy:
                        continue
                if any(s <= start < e for s, e in seen_spans):
                    continue
                seen_spans.append((start, end))
                matches.append(
                    SecretMatch(pattern.id, pattern.description, pattern.severity, lineno, start + 1, len(value))
                )
    return matches


class Redactor:
    """Removes known secret values and secret-looking tokens from text.

    Used for log files, history records and streamed output.
    """

    MIN_SECRET_LENGTH = 4

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()
        self._compiled: re.Pattern[str] | None = None
        self.add(secrets)

    def add(self, secrets: Iterable[str]) -> None:
        with self._lock:
            for value in secrets:
                if (
                    value
                    and len(value) >= self.MIN_SECRET_LENGTH
                    and not is_placeholder(value)
                    and not _looks_like_path(value)
                ):
                    self._values.add(value)
            self._compiled = (
                re.compile("|".join(re.escape(v) for v in sorted(self._values, key=len, reverse=True)))
                if self._values
                else None
            )

    def add_environment(self, env: Mapping[str, str]) -> None:
        """Register values of secret-looking variables from ``env``."""
        self.add(v for k, v in env.items() if is_secret_key(k))

    def redact(self, text: str) -> str:
        if not text:
            return text
        compiled = self._compiled
        if compiled is not None:
            text = compiled.sub(REDACTED, text)
        for pattern in SECRET_PATTERNS:
            if pattern.id in ("generic-secret-assignment",):
                continue
            text = _redact_pattern(pattern, text)
        return text


def _looks_like_path(value: str) -> bool:
    """Existing filesystem paths are not treated as secret values (they would mask output)."""
    if "://" in value or len(value) > 1024:
        return False
    try:
        from pathlib import Path

        return (value.startswith(("/", "~", ".")) or (len(value) > 2 and value[1] == ":")) and Path(
            value
        ).expanduser().exists()
    except (OSError, ValueError):
        return False


def _redact_pattern(pattern: SecretPattern, text: str) -> str:
    if "secret" not in pattern.regex.groupindex:
        return pattern.regex.sub(REDACTED, text)

    def _sub(m: re.Match[str]) -> str:
        whole = m.group(0)
        start, end = m.span("secret")
        offset = m.start(0)
        return whole[: start - offset] + REDACTED + whole[end - offset :]

    return pattern.regex.sub(_sub, text)
