"""Token issuance and verification.

Firebase authenticates the *user*; this module issues the credentials our own
API trusts. Keeping our own short-lived access tokens means a stolen token
expires in minutes, and means we can revoke a session without depending on
Firebase's revocation propagation.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt

from app.core.config import get_settings

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Raised when a token is malformed, expired, or of the wrong type."""


def _create_token(
    subject: str,
    token_type: TokenType,
    ttl_seconds: int,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)

    claims: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        # Unique token id, so an individual token can be denylisted in Redis
        # without invalidating every session the user has.
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        claims.update(extra_claims)

    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID, is_admin: bool = False) -> str:
    settings = get_settings()
    return _create_token(
        subject=str(user_id),
        token_type="access",
        ttl_seconds=settings.access_token_ttl_seconds,
        extra_claims={"admin": is_admin},
    )


def create_refresh_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    return _create_token(
        subject=str(user_id),
        token_type="refresh",
        ttl_seconds=settings.refresh_token_ttl_seconds,
    )


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a token, enforcing its declared type.

    The type check is essential: without it a long-lived refresh token would be
    accepted as an access token, silently defeating the short access TTL.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError(f"Token could not be validated: {exc}") from exc

    if payload.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type} token, received {payload.get('type')!r}")

    if "sub" not in payload:
        raise TokenError("Token is missing a subject claim")

    return payload


def generate_idempotency_key() -> str:
    """Cryptographically random key for client-retryable operations."""
    return secrets.token_urlsafe(32)
