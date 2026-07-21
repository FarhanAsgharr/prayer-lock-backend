"""Shared FastAPI dependencies: authentication, rate limiting, services."""

from __future__ import annotations

import uuid
from typing import Annotated

import redis
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import TokenError, decode_token
from app.db.session import get_db
from app.models.user import User
from app.providers.vision import build_vision_provider
from app.services.verification_service import VerificationService

logger = get_logger(__name__)

# auto_error=False so a missing header produces our own uniform 401 body
# rather than FastAPI's default shape.
bearer_scheme = HTTPBearer(auto_error=False)

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            str(get_settings().redis_url),
            decode_responses=True,
        )
    return _redis_client


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Resolve the caller from a bearer access token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is not a valid user id",
        ) from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found or disabled",
        )

    return user


def get_current_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Admin-only guard.

    The database flag is authoritative, not the token's `admin` claim: a token
    issued before a demotion must not retain admin access until it expires.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges are required",
        )
    return user


class RateLimiter:
    """Fixed-window rate limiter backed by Redis.

    Applied per user where the caller is authenticated, and per client IP
    otherwise. If Redis is unavailable the limiter allows the request rather
    than failing it — an outage in the limiter must not take down the API.
    """

    def __init__(self, *, limit: int, window_seconds: int, bucket: str) -> None:
        self._limit = limit
        self._window = window_seconds
        self._bucket = bucket

    async def __call__(
        self,
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    ) -> None:
        identity = self._identify(request, credentials)
        key = f"ratelimit:{self._bucket}:{identity}"

        try:
            client = get_redis()
            pipeline = client.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, self._window, nx=True)
            count, _ = pipeline.execute()
        except redis.RedisError as exc:
            logger.warning("rate_limiter_unavailable", bucket=self._bucket, error=str(exc))
            return

        if int(count) > self._limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again shortly.",
                headers={"Retry-After": str(self._window)},
            )

    @staticmethod
    def _identify(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str:
        if credentials is not None:
            try:
                payload = decode_token(credentials.credentials, expected_type="access")
                return f"user:{payload['sub']}"
            except (TokenError, KeyError):
                pass
        client_host = request.client.host if request.client else "unknown"
        return f"ip:{client_host}"


# Auth is the most attacked surface, so its window is tighter than the rest.
auth_rate_limit = RateLimiter(limit=10, window_seconds=60, bucket="auth")
# Verification calls a paid vision API, so abuse is expensive as well as noisy.
verification_rate_limit = RateLimiter(limit=20, window_seconds=300, bucket="verify")
general_rate_limit = RateLimiter(limit=120, window_seconds=60, bucket="general")


def get_verification_service() -> VerificationService:
    return VerificationService(provider=build_vision_provider())


def get_client_ip(request: Request) -> str | None:
    """Best-effort client IP.

    X-Forwarded-For is honoured only because the service is expected to run
    behind a trusted proxy that overwrites it. Exposed directly, this header is
    attacker-controlled and must not be trusted.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_user_agent(user_agent: Annotated[str | None, Header()] = None) -> str | None:
    return user_agent


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]
DbSession = Annotated[Session, Depends(get_db)]
