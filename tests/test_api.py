"""Endpoint tests covering routing, authorization and error handling."""

import base64
import io
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.models.enums import PrayerName, PrayerStatus
from app.models.prayer import PrayerHistory
from app.models.user import Location, User


def sample_image_base64() -> str:
    image = Image.new("RGB", (48, 48))
    pixels = image.load()
    assert pixels is not None
    for x in range(48):
        for y in range(48):
            pixels[x, y] = (x * 5 % 256, y * 3 % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class TestHealth:
    def test_health_reports_database_status(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"


class TestAuthorization:
    def test_prayer_times_requires_authentication(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.post("/api/v1/prayer-times", json={})
        assert response.status_code == 401

    def test_invalid_bearer_token_is_rejected(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.post(
            "/api/v1/prayer-times",
            json={},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401

    def test_verification_requires_authentication(self, unauthenticated_client: TestClient) -> None:
        response = unauthenticated_client.post(
            "/api/v1/verifications",
            json={"prayer_history_id": str(uuid.uuid4()), "image_base64": "x" * 100},
        )
        assert response.status_code == 401


class TestPrayerTimesEndpoint:
    def test_explicit_coordinates_return_a_schedule(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/prayer-times",
            json={
                "prayer_date": "2026-07-20",
                "latitude": 21.4225,
                "longitude": 39.8262,
                "timezone": "Asia/Riyadh",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["prayer_date"] == "2026-07-20"
        for prayer in ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"):
            assert body[prayer] is not None

    def test_missing_location_returns_helpful_error(self, client: TestClient) -> None:
        """A user with no saved location gets guidance, not a 500."""
        response = client.post("/api/v1/prayer-times", json={"prayer_date": "2026-07-20"})
        assert response.status_code == 400
        assert "location" in response.json()["detail"].lower()

    def test_active_location_is_used_when_coordinates_omitted(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        db.add(
            Location(
                user_id=user.id,
                label="Home",
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                is_active=True,
            )
        )
        db.flush()

        response = client.post("/api/v1/prayer-times", json={"prayer_date": "2026-07-20"})
        assert response.status_code == 200
        assert response.json()["timezone"] == "Asia/Riyadh"

    def test_out_of_range_latitude_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/prayer-times",
            json={"latitude": 200.0, "longitude": 0.0, "timezone": "UTC"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_madhab_override_changes_asr(self, client: TestClient) -> None:
        def asr_for(madhab: str) -> str:
            response = client.post(
                "/api/v1/prayer-times",
                json={
                    "prayer_date": "2026-07-20",
                    "latitude": 24.8607,
                    "longitude": 67.0011,
                    "timezone": "Asia/Karachi",
                    "madhab": madhab,
                },
            )
            assert response.status_code == 200
            return response.json()["asr"]

        assert asr_for("hanafi") > asr_for("shafi")


class TestVerificationEndpoint:
    def _create_prayer(self, db: Session, user: User) -> PrayerHistory:
        now = datetime.now(UTC)
        record = PrayerHistory(
            user_id=user.id,
            prayer_date=now.date(),
            prayer=PrayerName.DHUHR,
            status=PrayerStatus.ACTIVE,
            scheduled_at=now - timedelta(minutes=5),
            window_ends_at=now + timedelta(hours=3),
        )
        db.add(record)
        db.flush()
        return record

    def test_successful_verification_marks_prayer_complete(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        prayer = self._create_prayer(db, user)

        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(prayer.id),
                "image_base64": sample_image_base64(),
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["approved"] is True

        db.refresh(prayer)
        assert prayer.status is PrayerStatus.COMPLETED
        assert prayer.completed_at is not None
        assert prayer.delay_minutes is not None

    def test_unknown_prayer_returns_not_found(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(uuid.uuid4()),
                "image_base64": sample_image_base64(),
            },
        )
        assert response.status_code == 404

    def test_another_users_prayer_is_indistinguishable_from_missing(
        self, client: TestClient, db: Session, admin_user: User
    ) -> None:
        """Must not leak which prayer ids exist via a different status code."""
        others_prayer = self._create_prayer(db, admin_user)

        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(others_prayer.id),
                "image_base64": sample_image_base64(),
            },
        )
        assert response.status_code == 404

    def test_already_completed_prayer_is_rejected(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        prayer = self._create_prayer(db, user)
        prayer.status = PrayerStatus.COMPLETED
        db.flush()

        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(prayer.id),
                "image_base64": sample_image_base64(),
            },
        )
        assert response.status_code == 409

    def test_late_completion_is_recorded_as_late(
        self, client: TestClient, db: Session, user: User
    ) -> None:
        now = datetime.now(UTC)
        prayer = PrayerHistory(
            user_id=user.id,
            prayer_date=now.date(),
            prayer=PrayerName.FAJR,
            status=PrayerStatus.ACTIVE,
            scheduled_at=now - timedelta(hours=5),
            # Window already closed.
            window_ends_at=now - timedelta(hours=1),
        )
        db.add(prayer)
        db.flush()

        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(prayer.id),
                "image_base64": sample_image_base64(),
            },
        )
        assert response.status_code == 200

        db.refresh(prayer)
        assert prayer.status is PrayerStatus.LATE
        assert prayer.delay_minutes >= 299

    def test_oversized_payload_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/verifications",
            json={
                "prayer_history_id": str(uuid.uuid4()),
                "image_base64": "A" * 9_000_000,
            },
        )
        assert response.status_code == 422


class TestErrorEnvelope:
    def test_validation_errors_do_not_echo_the_payload(self, client: TestClient) -> None:
        """The error body must not contain the submitted image data.

        Pydantic's default handler echoes offending input, which for this API
        would mean base64 image payloads in logs and error responses.
        """
        secret_marker = "SENSITIVE" + "A" * 200
        response = client.post(
            "/api/v1/verifications",
            json={"prayer_history_id": "not-a-uuid", "image_base64": secret_marker},
        )
        assert response.status_code == 422
        assert secret_marker not in response.text

        body = response.json()
        assert body["code"] == "validation_error"
        assert any(
            field["field"].endswith("prayer_history_id") for field in body["detail"]["fields"]
        )
