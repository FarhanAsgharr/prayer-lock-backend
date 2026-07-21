"""Token issuance and validation tests."""

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)


class TestTokenIssuance:
    def test_access_token_round_trips(self) -> None:
        user_id = uuid.uuid4()
        payload = decode_token(create_access_token(user_id), expected_type="access")
        assert payload["sub"] == str(user_id)
        assert payload["type"] == "access"

    def test_admin_claim_is_carried(self) -> None:
        payload = decode_token(
            create_access_token(uuid.uuid4(), is_admin=True), expected_type="access"
        )
        assert payload["admin"] is True

    def test_each_token_has_a_unique_id(self) -> None:
        """Distinct jti values are what allow a single session to be revoked."""
        user_id = uuid.uuid4()
        first = decode_token(create_access_token(user_id), expected_type="access")
        second = decode_token(create_access_token(user_id), expected_type="access")
        assert first["jti"] != second["jti"]


class TestTokenTypeConfusion:
    def test_refresh_token_is_rejected_as_access_token(self) -> None:
        """The critical check: a long-lived token must not act as a short one.

        Without the type assertion, a stolen refresh token would grant API
        access for its full 30-day lifetime.
        """
        refresh = create_refresh_token(uuid.uuid4())
        with pytest.raises(TokenError, match="Expected a access token"):
            decode_token(refresh, expected_type="access")

    def test_access_token_is_rejected_as_refresh_token(self) -> None:
        access = create_access_token(uuid.uuid4())
        with pytest.raises(TokenError, match="Expected a refresh token"):
            decode_token(access, expected_type="refresh")


class TestTokenTampering:
    def test_token_signed_with_wrong_secret_is_rejected(self) -> None:
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "access",
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "an-attacker-chosen-secret",
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            decode_token(forged, expected_type="access")

    def test_expired_token_is_rejected(self) -> None:
        settings = get_settings()
        expired = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "access",
                "exp": int((datetime.now(UTC) - timedelta(seconds=5)).timestamp()),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(TokenError):
            decode_token(expired, expected_type="access")

    def test_token_without_subject_is_rejected(self) -> None:
        settings = get_settings()
        token = jwt.encode(
            {
                "type": "access",
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(TokenError, match="subject"):
            decode_token(token, expected_type="access")

    def test_garbage_input_is_rejected(self) -> None:
        with pytest.raises(TokenError):
            decode_token("not-a-jwt", expected_type="access")

    def test_none_algorithm_token_is_rejected(self) -> None:
        """Guards against the classic `alg: none` downgrade attack.

        The token is assembled by hand rather than with jose, because jose
        refuses to *encode* an unsigned token — but an attacker has no such
        scruples, and would craft the bytes directly as we do here.
        """

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        claims = b64(
            json.dumps(
                {
                    "sub": str(uuid.uuid4()),
                    "type": "access",
                    "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                }
            ).encode()
        )
        # Empty signature segment: the signature the attack relies on omitting.
        unsigned = f"{header}.{claims}."

        with pytest.raises(TokenError):
            decode_token(unsigned, expected_type="access")


class TestTokenLifetime:
    def test_access_token_expiry_matches_configuration(self) -> None:
        settings = get_settings()
        issued_at = int(time.time())
        payload = decode_token(create_access_token(uuid.uuid4()), expected_type="access")
        lifetime = payload["exp"] - issued_at
        assert lifetime == pytest.approx(settings.access_token_ttl_seconds, abs=2)

    def test_refresh_token_outlives_access_token(self) -> None:
        user_id = uuid.uuid4()
        access = decode_token(create_access_token(user_id), expected_type="access")
        refresh = decode_token(create_refresh_token(user_id), expected_type="refresh")
        assert refresh["exp"] > access["exp"]
