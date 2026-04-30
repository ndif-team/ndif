"""Authentication helpers — bcrypt + signed-cookie session.

Keeps the design intentionally minimal:
- Single admin (``DASHBOARD_USERNAME`` + ``DASHBOARD_PASSWORD_HASH``).
- Login returns an HttpOnly signed cookie (``itsdangerous.URLSafeTimedSerializer``).
- ``require_auth`` FastAPI dependency reads the cookie and 401s if missing/expired.

This module is also runnable as a CLI to generate a bcrypt hash:
    python -m ndif.services.dashboard.backend.auth hash <password>
"""

from __future__ import annotations

import sys
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings, get_settings


COOKIE_NAME = "ndif_dashboard_session"

# bcrypt operates on the first 72 bytes of the password; trim explicitly so
# very long inputs don't error out (bcrypt 4.x stopped truncating silently).
_BCRYPT_MAX_BYTES = 72


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="ndif-dashboard")


def _to_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def issue_session_token(username: str, settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    return _serializer(settings).dumps({"u": username})


def parse_session_token(token: str, settings: Optional[Settings] = None) -> Optional[str]:
    """Return the username embedded in ``token`` or ``None`` if invalid/expired."""
    settings = settings or get_settings()
    max_age = settings.session_ttl_days * 24 * 60 * 60
    try:
        data = _serializer(settings).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("u")


def require_auth(request: Request) -> str:
    """FastAPI dependency: return the authenticated username or 401."""
    settings = get_settings()
    if settings.dev_mode:
        return settings.username

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    username = parse_session_token(token, settings)
    if not username or username != settings.username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    return username


# ---------------------------------------------------------------------------
# CLI helper: ``python -m ... auth hash <password>``
# ---------------------------------------------------------------------------

def _cli() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
        return
    print("usage: python -m ndif.services.dashboard.backend.auth hash <password>",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    _cli()
