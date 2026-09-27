"""Password hashing, token generation and simple abuse protection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from highhx_platform.db import Database
from highhx_platform.models import RateLimitCounter

TOKEN_PREFIX = "hhx_"  # nosec B105 - a public prefix, not a credential
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels or look-alikes
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_device_code() -> str:
    return secrets.token_urlsafe(32)


def new_user_code() -> str:
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(code: str) -> str:
    raw = "".join(c for c in code.upper() if c.isalnum())
    return f"{raw[:4]}-{raw[4:8]}" if len(raw) == 8 else raw


def constant_time_equals(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


class RateLimiter:
    """Fixed-window limiter keyed by an arbitrary string (IP, email …).

    Counters live in the database (``rate_limit_counters``), so the limit holds across every
    platform instance. The increment is a single atomic UPDATE (or INSERT for a new window).
    """

    def __init__(self, db: Database, name: str, limit: int, window: int) -> None:
        self.db = db
        self.name = name
        self.limit = limit
        self.window = window

    def _key(self, key: str) -> str:
        return f"{self.name}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}"

    def allow(self, key: str) -> bool:
        bucket = self._key(key)
        start = int(time.time()) // self.window * self.window
        for _ in range(3):
            with self.db.sessions() as session:
                bumped = session.execute(
                    update(RateLimitCounter)
                    .where(RateLimitCounter.key == bucket, RateLimitCounter.window_start == start)
                    .values(count=RateLimitCounter.count + 1)
                    .returning(RateLimitCounter.count)
                ).scalar_one_or_none()
                if bumped is None:
                    session.add(RateLimitCounter(key=bucket, window_start=start, count=1))
                    try:
                        session.commit()
                    except IntegrityError:  # another instance created the window first: bump it
                        session.rollback()
                        continue
                    self._cleanup(start)
                    return self.limit >= 1
                session.commit()
                return int(bumped) <= self.limit
        return False

    def reset(self, key: str) -> None:
        with self.db.sessions() as session:
            session.execute(delete(RateLimitCounter).where(RateLimitCounter.key == self._key(key)))
            session.commit()

    def _cleanup(self, current: int) -> None:
        with self.db.sessions() as session:
            session.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.key.startswith(f"{self.name}:"), RateLimitCounter.window_start < current
                )
            )
            session.commit()


class Limits:
    """Rate limiters shared by every instance of the platform (through its database)."""

    def __init__(self, db: Database, *, login: int, signup: int, device: int) -> None:
        self.login = RateLimiter(db, "login", limit=login, window=900)
        self.signup = RateLimiter(db, "signup", limit=signup, window=3600)
        self.device = RateLimiter(db, "device", limit=device, window=3600)
