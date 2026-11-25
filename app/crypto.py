"""Encryption at rest for per-business secrets (Daraja keys).

Key: DUKA_ENCRYPTION_KEY (a Fernet key) if set, otherwise derived from DUKA_SECRET_KEY.
Rotating DUKA_SECRET_KEY without setting DUKA_ENCRYPTION_KEY makes stored secrets unreadable —
set DUKA_ENCRYPTION_KEY explicitly in production.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY


def _fernet() -> Fernet:
    key = os.getenv("DUKA_ENCRYPTION_KEY")
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("duka-enc:" + SECRET_KEY).encode()).digest()).decode()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def mask(value: str) -> str:
    return "" if not value else ("•" * 6 + value[-4:] if len(value) > 4 else "••••")
