"""Password hashing + JWT token utilities."""
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt

from app.config import settings


# ─────────────────────────── Password hashing ─────────────────────────────

def hash_password(plain: str) -> str:
    """bcrypt hash. Cost=12 (~250ms on commodity hardware)."""
    if not plain:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ─────────────────────────────── JWT ──────────────────────────────────────

class TokenError(Exception):
    pass


def _require_secret() -> str:
    if not settings.jwt_secret:
        raise TokenError(
            "jwt_secret not configured. Set JWT_SECRET in .env "
            "(generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")"
        )
    return settings.jwt_secret


def create_access_token(subject: str, role: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expires_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _require_secret(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _require_secret(), algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise TokenError("Token expired")
    except jwt.InvalidTokenError as e:
        raise TokenError(f"Invalid token: {e}")
