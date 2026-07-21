"""Authentication endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DbSession, auth_rate_limit, get_client_ip, get_user_agent
from app.core.config import get_settings
from app.schemas.auth import (
    AuthResponse,
    FirebaseExchangeRequest,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthenticationError, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
auth_service = AuthService()


@router.post(
    "/firebase",
    response_model=AuthResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
    summary="Exchange a Firebase ID token for API credentials",
)
def exchange_firebase_token(
    payload: FirebaseExchangeRequest,
    request: Request,
    db: DbSession,
    user_agent: Annotated[str | None, Depends(get_user_agent)],
) -> AuthResponse:
    """Verify a Firebase ID token and issue our own access/refresh pair.

    The user is provisioned on first sign-in, along with default settings.
    """
    try:
        user, tokens = auth_service.exchange_firebase_token(
            db,
            id_token=payload.id_token,
            timezone=payload.timezone,
            ip_address=get_client_ip(request),
            user_agent=user_agent,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    db.commit()

    return AuthResponse(
        user=UserResponse.model_validate(user),
        tokens=TokenResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=get_settings().access_token_ttl_seconds,
        ),
    )


@router.post(
    "/refresh",
    response_model=AuthResponse,
    dependencies=[Depends(auth_rate_limit)],
    summary="Exchange a refresh token for a new access token",
)
def refresh_tokens(
    payload: RefreshRequest,
    request: Request,
    db: DbSession,
) -> AuthResponse:
    try:
        user, tokens = auth_service.refresh(
            db,
            refresh_token=payload.refresh_token,
            ip_address=get_client_ip(request),
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    db.commit()

    return AuthResponse(
        user=UserResponse.model_validate(user),
        tokens=TokenResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=get_settings().access_token_ttl_seconds,
        ),
    )
