"""Password hashing, signed session tokens, role checks and login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .config import (
    COOKIE_SECURE, LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS, SECRET_KEY, SESSION_DAYS,
)
from .db import AuditLog, User, db_session

PBKDF2_ITERATIONS = 600_000  # OWASP 2023+ guidance for PBKDF2-HMAC-SHA256
LEGACY_ITERATIONS = 240_000  # v0.1 hashes; upgraded transparently on next login
COOKIE_NAME = "duka_session"


# ---------------------------------------------------------------- passwords
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode()
    return f"pbkdf2${iterations}${b64(salt)}${b64(digest)}"


def _parse_hash(encoded: str) -> tuple[int, bytes, bytes]:
    if encoded.startswith("pbkdf2$"):
        _, it, s, d = encoded.split("$")
        return int(it), base64.urlsafe_b64decode(s), base64.urlsafe_b64decode(d)
    s, d = encoded.split(".")  # v0.1 format: salt.digest
    return LEGACY_ITERATIONS, base64.urlsafe_b64decode(s), base64.urlsafe_b64decode(d)


def verify_password(password: str, encoded: str) -> bool:
    try:
        iterations, salt, expected = _parse_hash(encoded)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def needs_rehash(encoded: str) -> bool:
    try:
        return _parse_hash(encoded)[0] < PBKDF2_ITERATIONS
    except Exception:
        return True


# Burn equivalent CPU when the email does not exist so response time does not reveal registered emails
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def dummy_verify() -> None:
    verify_password("not-the-password", _DUMMY_HASH)


# ---------------------------------------------------------------- tokens
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(raw: str) -> str:
    return _b64(hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).digest())


def token_for(user: User) -> str:
    payload = {
        "uid": user.id, "bid": user.business_id, "tv": user.token_version or 0,
        "exp": int(time.time()) + 60 * 60 * 24 * SESSION_DAYS,
    }
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return raw + "." + _sign(raw)


def set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        COOKIE_NAME, token_for(user), httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=60 * 60 * 24 * SESSION_DAYS, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=COOKIE_SECURE)


def current_user(request: Request, db: Session = Depends(db_session)) -> User:
    auth = request.headers.get("authorization", "")
    token = request.cookies.get(COOKIE_NAME) or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
    if not token:
        raise HTTPException(401, "Please sign in")
    try:
        raw, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(raw)):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload["exp"] < time.time():
            raise ValueError("expired")
        user = db.get(User, int(payload["uid"]))
        if (not user or not user.active or user.business_id != int(payload["bid"])
                or int(payload.get("tv", 0)) != (user.token_version or 0)):
            raise ValueError("revoked")
        return user
    except Exception:
        raise HTTPException(401, "Session expired. Please sign in again.")


def require_roles(*roles: str) -> Callable[..., User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Your role does not have access to this action")
        return user
    return dependency


require_owner = require_roles("owner")
require_manager = require_roles("owner", "manager")


def audit(db: Session, user: User, action: str, entity: str = "", entity_id: int | None = None, detail: str = "") -> None:
    db.add(AuditLog(business_id=user.business_id, user_id=user.id, action=action,
                    entity=entity, entity_id=entity_id, detail=detail[:2000]))


# ---------------------------------------------------------------- login throttling
class LoginThrottle:
    """In-memory sliding-window limiter keyed by IP and by email.

    Per-process only: behind several workers/instances move this to Redis.
    """

    def __init__(self, max_failures: int, window: int):
        self.max_failures, self.window = max_failures, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        now = time.time()
        with self._lock:
            waits = [int(q[0] + self.window - now) + 1 for k in keys
                     if len(q := self._prune(k, now)) >= self.max_failures]
        return max(waits, default=0)

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._prune(k, now).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)


# Per account: strict. Per IP: looser, because mobile carriers put many customers behind one shared IP (CGNAT).
login_throttle = LoginThrottle(LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS)
ip_throttle = LoginThrottle(LOGIN_MAX_FAILURES * 6, LOGIN_WINDOW_SECONDS)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
