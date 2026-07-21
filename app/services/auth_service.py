"""Authentication: Firebase identity exchange and session issuance.

The mobile client authenticates with Firebase and receives an ID token. It
exchanges that token here for our own access/refresh pair. We verify the
Firebase token's signature on every exchange, provision the user on first
sight, and never accept an unverified token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.models.enums import AuditAction
from app.models.user import User, UserSettings
from app.services.audit_service import record_audit_event

logger = get_logger(__name__)


class AuthenticationError(Exception):
    """Credential was missing, invalid, or belongs to a disabled account."""


@dataclass(frozen=True)
class FirebaseIdentity:
    uid: str
    email: str | None
    display_name: str | None
    email_verified: bool


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class FirebaseVerifier:
    """Verifies Firebase ID tokens.

    Initialisation is lazy so the service starts without Firebase credentials
    in local development, where `verify` is not reached.
    """

    def __init__(self) -> None:
        self._app = None

    def _ensure_initialised(self) -> None:
        if self._app is not None:
            return

        settings = get_settings()
        if not settings.firebase_credentials_path:
            raise AuthenticationError("Firebase credentials are not configured on this server")

        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(settings.firebase_credentials_path)
        # Reuse an already-initialised default app if one exists; Firebase
        # raises rather than returning the existing instance.
        try:
            self._app = firebase_admin.initialize_app(cred)
        except ValueError:
            self._app = firebase_admin.get_app()

    def verify(self, id_token: str) -> FirebaseIdentity:
        self._ensure_initialised()

        from firebase_admin import auth as firebase_auth

        try:
            # check_revoked forces a lookup against Firebase's revocation list,
            # so a token invalidated by a password change or sign-out is
            # rejected rather than honoured until expiry.
            claims = firebase_auth.verify_id_token(id_token, check_revoked=True)
        except Exception as exc:  # firebase-admin raises a broad set of types
            raise AuthenticationError(f"Firebase token rejected: {exc}") from exc

        uid = claims.get("uid") or claims.get("sub")
        if not uid:
            raise AuthenticationError("Firebase token contained no subject")

        return FirebaseIdentity(
            uid=uid,
            email=claims.get("email"),
            display_name=claims.get("name"),
            email_verified=bool(claims.get("email_verified", False)),
        )


class AuthService:
    def __init__(self, verifier: FirebaseVerifier | None = None) -> None:
        self._verifier = verifier or FirebaseVerifier()

    def exchange_firebase_token(
        self,
        db: Session,
        *,
        id_token: str,
        timezone: str = "UTC",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, TokenPair]:
        identity = self._verifier.verify(id_token)
        user = self._get_or_provision_user(db, identity, timezone=timezone)

        if not user.is_active:
            raise AuthenticationError("This account has been disabled")

        user.last_seen_at = datetime.now(UTC)

        record_audit_event(
            db,
            user_id=user.id,
            action=AuditAction.LOGIN,
            detail={"firebase_uid": identity.uid},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return user, self._issue_tokens(user)

    def refresh(
        self,
        db: Session,
        *,
        refresh_token: str,
        ip_address: str | None = None,
    ) -> tuple[User, TokenPair]:
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
        except TokenError as exc:
            raise AuthenticationError(str(exc)) from exc

        try:
            user_id = uuid.UUID(payload["sub"])
        except (ValueError, KeyError) as exc:
            raise AuthenticationError("Refresh token subject is not a valid user id") from exc

        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account not found or disabled")

        record_audit_event(
            db,
            user_id=user.id,
            action=AuditAction.TOKEN_REFRESHED,
            ip_address=ip_address,
        )

        return user, self._issue_tokens(user)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _issue_tokens(user: User) -> TokenPair:
        return TokenPair(
            access_token=create_access_token(user.id, is_admin=user.is_admin),
            refresh_token=create_refresh_token(user.id),
        )

    @staticmethod
    def _get_or_provision_user(
        db: Session,
        identity: FirebaseIdentity,
        *,
        timezone: str,
    ) -> User:
        """Find the user by Firebase uid, creating them on first sign-in.

        Lookup is by `firebase_uid`, never by email: emails are mutable and
        reusable, and matching on one would let a recycled address inherit a
        previous owner's prayer history.
        """
        user = db.execute(
            select(User).where(User.firebase_uid == identity.uid)
        ).scalar_one_or_none()

        if user is not None:
            # Keep the profile fresh, but never downgrade a known value to null.
            if identity.email:
                user.email = identity.email
            if identity.display_name:
                user.display_name = identity.display_name
            return user

        user = User(
            firebase_uid=identity.uid,
            email=identity.email,
            display_name=identity.display_name,
            timezone=timezone,
        )
        # Every user needs settings; creating them here means no other code
        # path has to handle a user without them.
        user.settings = UserSettings()
        db.add(user)
        db.flush()

        logger.info("user_provisioned", user_id=str(user.id))
        return user
