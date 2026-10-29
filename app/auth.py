"""Optional single-password login.

Tokens are signed rather than kept in memory, so a service restart doesn't
sign out every browser and phone. Changing UI_PASSWORD invalidates them all,
because the password is part of what gets signed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional

from .config import DATA_DIR, settings

COOKIE = "ilo_session"
_KEY_FILE = DATA_DIR / "secret.key"


def _key() -> bytes:
    if not _KEY_FILE.exists():
        _KEY_FILE.write_bytes(secrets.token_bytes(32))
        try:
            _KEY_FILE.chmod(0o600)
        except OSError:
            pass
    return _KEY_FILE.read_bytes() + settings.ui_password.encode("utf-8")


def _sign(payload: str) -> str:
    mac = hmac.new(_key(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def required() -> bool:
    return bool(settings.ui_password)


def check_password(password: str) -> bool:
    return secrets.compare_digest(password.encode("utf-8"), settings.ui_password.encode("utf-8"))


def issue() -> str:
    exp = int(time.time()) + settings.session_days * 86400
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def valid(token: Optional[str]) -> bool:
    if not required():
        return True
    if not token or token.count(".") != 2:
        return False
    exp, nonce, mac = token.split(".")
    if not hmac.compare_digest(mac, _sign(f"{exp}.{nonce}")):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def max_age() -> int:
    return settings.session_days * 86400
