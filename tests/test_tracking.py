"""Tests for tracking uploads, idempotency, statistics and streaks.

The two properties that matter most here:

- Idempotency. The mobile sync queue guarantees at-least-once delivery, so
  every upload endpoint must be safe to call twice with the same client id.
  A duplicate that inserts a second row would corrupt the user's history and
  double-count their prayers.

- Streak parity. The streak rules mirror the mobile app's StreakCalculator
  exactly; a mismatch makes the number jump when the dashboard refreshes from
  the server. These tests encode the same cases the Dart tests do.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.enums import PlatformType, PrayerName, PrayerStatus
from app.models.prayer import PrayerHistory
from app.models.user import User
from app.schemas.tracking import (
    DeviceRegistrationRequest,
    EmergencyUnlockUpload,
    LockSessionUpload,
    PrayerHistoryUpload,
    VerificationRecordUpload,
)
from app.services.device_service import device_service
from app.services.tracking_service import tracking_service


def _history_payload(
    prayer: PrayerName = PrayerName.FAJR,
    status: PrayerStatus = PrayerStatus.COMPLETED,
    on: date | None = None,
) -> PrayerHistoryUpload:
    day = on or date(2026, 7, 20)
    scheduled = datetime(day.year, day.month, day.day, 4, 27, tzinfo=UTC)
    return PrayerHistoryUpload(
        client_id=f"{day.isoformat()}:{prayer.value}",
        prayer_date=day,
        prayer=prayer,
        status=status,
        scheduled_at=scheduled,
        window_ends_at=scheduled + timedelta(hours=1),
        completed_at=scheduled + timedelta(minutes=10)
        if status in (PrayerStatus.COMPLETED, PrayerStatus.LATE)
        else None,
        delay_minutes=10 if status is PrayerStatus.COMPLETED else None,
    )


class TestDeviceRegistration:
    def test_registers_a_new_device(self, db: Session, user: User) -> None:
        device = device_service.register(
            db,
            user_id=user.id,
            payload=DeviceRegistrationRequest(
                install_id="install-abcdef123",
                platform=PlatformType.ANDROID,
                fcm_token="token-1",
            ),
        )
        assert device.id is not None
        assert device.fcm_token == "token-1"

    def test_reregistration_updates_in_place(self, db: Session, user: User) -> None:
        # The app re-registers on every launch; this must not accumulate rows.
        payload = DeviceRegistrationRequest(
            install_id="install-stable",
            platform=PlatformType.ANDROID,
            fcm_token="token-old",
        )
        first = device_service.register(db, user_id=user.id, payload=payload)

        payload.fcm_token = "token-new"
        second = device_service.register(db, user_id=user.id, payload=payload)

        assert first.id == second.id
        assert second.fcm_token == "token-new"


class TestPrayerHistoryUpload:
    def test_uploads_a_prayer(self, db: Session, user: User) -> None:
        record, duplicate = tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=_history_payload()
        )
        assert duplicate is False
        assert record.status is PrayerStatus.COMPLETED

    def test_is_idempotent(self, db: Session, user: User) -> None:
        # The core at-least-once guarantee: the same upload twice yields one row.
        payload = _history_payload()
        first, dup1 = tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=payload
        )
        second, dup2 = tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=payload
        )

        assert dup1 is False
        assert dup2 is True
        assert first.id == second.id

        count = (
            db.query(PrayerHistory)
            .filter(PrayerHistory.user_id == user.id)
            .count()
        )
        assert count == 1

    def test_a_missed_prayer_can_be_upgraded_to_completed(
        self, db: Session, user: User
    ) -> None:
        # Offline reconciliation may record a prayer missed, then a later
        # completion arrives. The upgrade must be honoured.
        tracking_service.upsert_prayer_history(
            db,
            user_id=user.id,
            payload=_history_payload(status=PrayerStatus.MISSED),
        )
        record, duplicate = tracking_service.upsert_prayer_history(
            db,
            user_id=user.id,
            payload=_history_payload(status=PrayerStatus.COMPLETED),
        )

        assert duplicate is True
        assert record.status is PrayerStatus.COMPLETED

    def test_a_fulfilled_prayer_is_never_downgraded_to_missed(
        self, db: Session, user: User
    ) -> None:
        # A stale replay of an old "missed" must not erase a real completion.
        tracking_service.upsert_prayer_history(
            db,
            user_id=user.id,
            payload=_history_payload(status=PrayerStatus.COMPLETED),
        )
        record, _ = tracking_service.upsert_prayer_history(
            db,
            user_id=user.id,
            payload=_history_payload(status=PrayerStatus.MISSED),
        )

        assert record.status is PrayerStatus.COMPLETED


class TestVerificationUpload:
    def test_records_against_its_prayer(self, db: Session, user: User) -> None:
        prayer, _ = tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=_history_payload()
        )
        record, duplicate = tracking_service.record_verification(
            db,
            user_id=user.id,
            payload=VerificationRecordUpload(
                client_id="v1",
                prayer_history_id="2026-07-20:fajr",
                approved=True,
                attempt_number=1,
                created_at=datetime.now(UTC),
            ),
        )
        assert duplicate is False
        assert record.prayer_history_id == prayer.id

    def test_raises_when_the_prayer_is_missing(
        self, db: Session, user: User
    ) -> None:
        # The verification arrived before its prayer synced; the endpoint turns
        # this into a 409 so the client retries rather than losing the record.
        try:
            tracking_service.record_verification(
                db,
                user_id=user.id,
                payload=VerificationRecordUpload(
                    client_id="v1",
                    prayer_history_id="2026-07-20:fajr",
                    approved=True,
                    attempt_number=1,
                    created_at=datetime.now(UTC),
                ),
            )
            raise AssertionError("expected LookupError")
        except LookupError:
            pass

    def test_is_idempotent_per_attempt(self, db: Session, user: User) -> None:
        tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=_history_payload()
        )
        payload = VerificationRecordUpload(
            client_id="v1",
            prayer_history_id="2026-07-20:fajr",
            approved=True,
            attempt_number=1,
            created_at=datetime.now(UTC),
        )
        first, dup1 = tracking_service.record_verification(
            db, user_id=user.id, payload=payload
        )
        second, dup2 = tracking_service.record_verification(
            db, user_id=user.id, payload=payload
        )

        assert dup1 is False
        assert dup2 is True
        assert first.id == second.id


class TestLockSessionUpload:
    def test_records_a_session(self, db: Session, user: User) -> None:
        record, duplicate = tracking_service.record_lock_session(
            db,
            user_id=user.id,
            payload=LockSessionUpload(
                client_id="s1",
                prayer=PrayerName.FAJR,
                started_at=datetime(2026, 7, 20, 4, 27, tzinfo=UTC),
                ended_at=datetime(2026, 7, 20, 4, 45, tzinfo=UTC),
                end_reason="verified",
                blocked_app_count=3,
            ),
        )
        assert duplicate is False
        assert record.end_reason == "verified"

    def test_is_idempotent(self, db: Session, user: User) -> None:
        payload = LockSessionUpload(
            client_id="s1",
            prayer=PrayerName.FAJR,
            started_at=datetime(2026, 7, 20, 4, 27, tzinfo=UTC),
            ended_at=datetime(2026, 7, 20, 4, 45, tzinfo=UTC),
            end_reason="verified",
        )
        first, dup1 = tracking_service.record_lock_session(
            db, user_id=user.id, payload=payload
        )
        second, dup2 = tracking_service.record_lock_session(
            db, user_id=user.id, payload=payload
        )
        assert dup1 is False
        assert dup2 is True
        assert first.id == second.id


class TestEmergencyUnlockUpload:
    def test_records_an_unlock(self, db: Session, user: User) -> None:
        record, duplicate = tracking_service.record_emergency_unlock(
            db,
            user_id=user.id,
            payload=EmergencyUnlockUpload(
                client_id="e1",
                unlock_date=date(2026, 7, 20),
                daily_sequence=1,
                reason="urgent call",
                created_at=datetime.now(UTC),
            ),
        )
        assert duplicate is False
        assert record.daily_sequence == 1

    def test_is_idempotent_on_the_daily_sequence(
        self, db: Session, user: User
    ) -> None:
        # A retry must not consume a second unlock against the daily quota.
        payload = EmergencyUnlockUpload(
            client_id="e1",
            unlock_date=date(2026, 7, 20),
            daily_sequence=1,
            created_at=datetime.now(UTC),
        )
        _, dup1 = tracking_service.record_emergency_unlock(
            db, user_id=user.id, payload=payload
        )
        _, dup2 = tracking_service.record_emergency_unlock(
            db, user_id=user.id, payload=payload
        )
        assert dup1 is False
        assert dup2 is True


class TestStatistics:
    def test_empty_user_has_zero_streak(self, db: Session, user: User) -> None:
        stats = tracking_service.statistics(db, user_id=user.id)
        assert stats.streak.current == 0
        assert stats.all_time.completed == 0

    def test_counts_by_prayer(self, db: Session, user: User) -> None:
        for prayer in (PrayerName.FAJR, PrayerName.DHUHR):
            tracking_service.upsert_prayer_history(
                db, user_id=user.id, payload=_history_payload(prayer=prayer)
            )

        stats = tracking_service.statistics(db, user_id=user.id)
        assert stats.by_prayer[PrayerName.FAJR].completed == 1
        assert stats.by_prayer[PrayerName.DHUHR].completed == 1

    def test_success_rate_excludes_excused(self, db: Session, user: User) -> None:
        # An excused prayer must not inflate the rate, or the number becomes
        # meaningless.
        base = date(2026, 7, 20)
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(PrayerName.FAJR, PrayerStatus.COMPLETED, base),
        )
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(PrayerName.DHUHR, PrayerStatus.MISSED, base),
        )
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(PrayerName.ASR, PrayerStatus.EXCUSED, base),
        )

        stats = tracking_service.statistics(db, user_id=user.id, today=base)
        # 1 completed of 2 assessed (excused excluded) = 0.5.
        assert stats.all_time.success_rate == 0.5


class TestQaza:
    """The verification-window / qaza / missed model, server side."""

    def test_qaza_is_counted_separately_from_on_time(
        self, db: Session, user: User
    ) -> None:
        base = date(2026, 7, 20)
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(PrayerName.FAJR, PrayerStatus.COMPLETED, base),
        )
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(
                PrayerName.DHUHR, PrayerStatus.QAZA_COMPLETED, base
            ),
        )

        stats = tracking_service.statistics(db, user_id=user.id, today=base)
        assert stats.all_time.completed == 1
        assert stats.all_time.qaza == 1
        assert stats.by_prayer[PrayerName.DHUHR].qaza == 1

    def test_qaza_counts_towards_success_but_not_on_time(
        self, db: Session, user: User
    ) -> None:
        base = date(2026, 7, 20)
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(
                PrayerName.FAJR, PrayerStatus.QAZA_COMPLETED, base
            ),
        )
        tracking_service.upsert_prayer_history(
            db, user_id=user.id,
            payload=_history_payload(PrayerName.DHUHR, PrayerStatus.MISSED, base),
        )

        stats = tracking_service.statistics(db, user_id=user.id, today=base)
        # 1 qaza fulfilled of 2 assessed = 0.5 success; 0 on time.
        assert stats.all_time.success_rate == 0.5
        assert stats.all_time.completed == 0

    def test_a_qaza_day_keeps_the_streak(self, db: Session, user: User) -> None:
        # A make-up prayer must not break the streak — only a true miss does.
        today = date(2026, 7, 20)
        for prayer in PrayerName:
            status = (
                PrayerStatus.QAZA_COMPLETED
                if prayer is PrayerName.FAJR
                else PrayerStatus.COMPLETED
            )
            tracking_service.upsert_prayer_history(
                db, user_id=user.id,
                payload=_history_payload(prayer, status, today),
            )

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.current == 1

    def test_window_columns_persist(self, db: Session, user: User) -> None:
        # The deadlines the client sends must be stored, per the spec's
        # database requirements.
        base = date(2026, 7, 20)
        scheduled = datetime(base.year, base.month, base.day, 4, 27, tzinfo=UTC)
        payload = PrayerHistoryUpload(
            client_id=f"{base.isoformat()}:fajr",
            prayer_date=base,
            prayer=PrayerName.FAJR,
            status=PrayerStatus.COMPLETED,
            scheduled_at=scheduled,
            window_ends_at=scheduled + timedelta(minutes=90),
            verification_deadline=scheduled + timedelta(minutes=30),
            qaza_deadline=scheduled + timedelta(minutes=90),
            completed_at=scheduled + timedelta(minutes=10),
        )
        record, _ = tracking_service.upsert_prayer_history(
            db, user_id=user.id, payload=payload
        )
        assert record.verification_deadline == scheduled + timedelta(minutes=30)
        assert record.qaza_deadline == scheduled + timedelta(minutes=90)


class TestStreakParity:
    """Mirrors the mobile app's StreakCalculator test cases exactly."""

    def _perfect_day(self, db: Session, user: User, on: date) -> None:
        for prayer in PrayerName:
            tracking_service.upsert_prayer_history(
                db, user_id=user.id,
                payload=_history_payload(prayer, PrayerStatus.COMPLETED, on),
            )

    def test_consecutive_perfect_days_ending_today(
        self, db: Session, user: User
    ) -> None:
        today = date(2026, 7, 20)
        for offset in range(3):
            self._perfect_day(db, user, today - timedelta(days=offset))

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.current == 3

    def test_streak_ending_yesterday_still_counts(
        self, db: Session, user: User
    ) -> None:
        # Today's prayers are not all done yet; yesterday anchors the streak.
        today = date(2026, 7, 20)
        self._perfect_day(db, user, today - timedelta(days=1))
        self._perfect_day(db, user, today - timedelta(days=2))

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.current == 2

    def test_a_late_prayer_keeps_the_streak(
        self, db: Session, user: User
    ) -> None:
        today = date(2026, 7, 20)
        for prayer in PrayerName:
            status = (
                PrayerStatus.LATE
                if prayer is PrayerName.FAJR
                else PrayerStatus.COMPLETED
            )
            tracking_service.upsert_prayer_history(
                db, user_id=user.id,
                payload=_history_payload(prayer, status, today),
            )

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.current == 1

    def test_a_missed_prayer_breaks_the_day(
        self, db: Session, user: User
    ) -> None:
        today = date(2026, 7, 20)
        for prayer in PrayerName:
            status = (
                PrayerStatus.MISSED
                if prayer is PrayerName.FAJR
                else PrayerStatus.COMPLETED
            )
            tracking_service.upsert_prayer_history(
                db, user_id=user.id,
                payload=_history_payload(prayer, status, today),
            )

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.current == 0

    def test_longest_streak_survives_a_later_break(
        self, db: Session, user: User
    ) -> None:
        today = date(2026, 7, 20)
        # A 4-day run ending 5 days ago, then a break, then today.
        for offset in range(2, 6):
            self._perfect_day(db, user, today - timedelta(days=offset))
        self._perfect_day(db, user, today)

        stats = tracking_service.statistics(db, user_id=user.id, today=today)
        assert stats.streak.longest == 4
        assert stats.streak.current == 1


class TestEndpoints:
    """End-to-end through the HTTP layer with an authenticated client."""

    def test_upload_and_read_statistics(
        self, client, db: Session, user: User
    ) -> None:
        response = client.post(
            "/api/v1/prayer-history",
            json={
                "client_id": "2026-07-20:fajr",
                "prayer_date": "2026-07-20",
                "prayer": "fajr",
                "status": "completed",
                "scheduled_at": "2026-07-20T04:27:00Z",
                "window_ends_at": "2026-07-20T05:49:00Z",
                "completed_at": "2026-07-20T04:37:00Z",
                "delay_minutes": 10,
            },
        )
        assert response.status_code == 200
        assert response.json()["duplicate"] is False

        # Re-upload: same client id, must be flagged duplicate not re-inserted.
        again = client.post(
            "/api/v1/prayer-history",
            json={
                "client_id": "2026-07-20:fajr",
                "prayer_date": "2026-07-20",
                "prayer": "fajr",
                "status": "completed",
                "scheduled_at": "2026-07-20T04:27:00Z",
                "window_ends_at": "2026-07-20T05:49:00Z",
            },
        )
        assert again.json()["duplicate"] is True

        stats = client.get("/api/v1/statistics")
        assert stats.status_code == 200
        assert stats.json()["by_prayer"]["fajr"]["completed"] == 1

    def test_device_registration_endpoint(self, client) -> None:
        response = client.post(
            "/api/v1/devices",
            json={"install_id": "install-endpoint", "platform": "android"},
        )
        assert response.status_code == 200
        assert response.json()["platform"] == "android"

    def test_endpoints_require_authentication(
        self, unauthenticated_client
    ) -> None:
        response = unauthenticated_client.post("/api/v1/prayer-history", json={})
        assert response.status_code == 401
