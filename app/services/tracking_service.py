"""Server-side prayer tracking, statistics and streaks.

The streak rules here are a deliberate mirror of the mobile app's
`StreakCalculator` (mobile/lib/features/tracking/domain/usecases/
streak_calculator.dart). If the server and device disagree about a streak, the
number visibly jumps when the dashboard refreshes from the server — so the two
must stay in lockstep. The parity is covered by tests on both sides.

Two rules that look lenient and are intentional:

- A late prayer keeps the streak. Breaking a long streak over a prayer that was
  performed, merely late, teaches the user the app is not worth the effort.
- An excused prayer keeps the streak, and is excluded from the success rate
  entirely. Menstruation, illness and travel are exemptions in fiqh, not
  failures.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.blocking import EmergencyUnlock, LockSession
from app.models.enums import (
    AuditAction,
    PrayerName,
    PrayerStatus,
    VerificationStatus,
)
from app.models.prayer import PrayerHistory
from app.models.verification import PrayerVerification
from app.schemas.tracking import (
    DashboardStatistics,
    EmergencyUnlockUpload,
    LockSessionUpload,
    PeriodCounts,
    PrayerHistoryUpload,
    StreakResponse,
    VerificationRecordUpload,
)
from app.services.audit_service import record_audit_event

logger = get_logger(__name__)

# Statuses that count as the obligation discharged (qaza and legacy late
# included — a make-up prayer keeps the streak, only a true miss breaks it).
_FULFILLED = frozenset(
    {
        PrayerStatus.COMPLETED,
        PrayerStatus.QAZA_COMPLETED,
        PrayerStatus.LATE,
        PrayerStatus.EXCUSED,
    }
)


@dataclass
class _Counts:
    completed: int = 0  # verified on time
    qaza: int = 0  # verified as qaza
    missed: int = 0
    excused: int = 0

    def add(self, status: PrayerStatus) -> None:
        if status is PrayerStatus.COMPLETED:
            self.completed += 1
        elif status in (PrayerStatus.QAZA_COMPLETED, PrayerStatus.LATE):
            # Legacy LATE is folded into qaza — both mean "performed after the
            # on-time window".
            self.qaza += 1
        elif status is PrayerStatus.MISSED:
            self.missed += 1
        elif status is PrayerStatus.EXCUSED:
            self.excused += 1

    @property
    def assessed(self) -> int:
        return self.completed + self.qaza + self.missed

    @property
    def fulfilled(self) -> int:
        return self.completed + self.qaza

    def to_schema(self) -> PeriodCounts:
        # A period with nothing assessed yet is reported as 1.0, matching the
        # client: showing 0% to someone who has not had the chance to pray reads
        # as an accusation.
        rate = 1.0 if self.assessed == 0 else self.fulfilled / self.assessed
        return PeriodCounts(
            completed=self.completed,
            qaza=self.qaza,
            missed=self.missed,
            excused=self.excused,
            success_rate=rate,
        )


@dataclass
class _DailySummary:
    day: date
    counts: _Counts = field(default_factory=_Counts)

    @property
    def is_perfect(self) -> bool:
        return self.counts.assessed > 0 and self.counts.missed == 0


class TrackingService:
    # --- Uploads ---------------------------------------------------------
    #
    # Each upload is idempotent on (user_id, client_id): a retried request
    # updates the existing row rather than inserting a duplicate. The mobile
    # sync queue guarantees at-least-once delivery, so the server must be safe
    # under at-least-once.

    def upsert_prayer_history(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        payload: PrayerHistoryUpload,
    ) -> tuple[PrayerHistory, bool]:
        existing = db.execute(
            select(PrayerHistory).where(
                PrayerHistory.user_id == user_id,
                PrayerHistory.prayer_date == payload.prayer_date,
                PrayerHistory.prayer == payload.prayer,
            )
        ).scalar_one_or_none()

        if existing is not None:
            # History is append-mostly, but a prayer completed after first
            # being recorded missed (offline reconciliation, then a late
            # completion) is a legitimate update. Never downgrade a fulfilled
            # prayer back to missed, though — that could only be a stale replay.
            if existing.status in _FULFILLED and payload.status is PrayerStatus.MISSED:
                return existing, True
            existing.status = payload.status
            existing.completed_at = payload.completed_at
            existing.qaza_completed_at = payload.qaza_completed_at
            existing.verification_deadline = payload.verification_deadline
            existing.qaza_deadline = payload.qaza_deadline
            existing.delay_minutes = payload.delay_minutes
            existing.excuse_reason = payload.excuse_reason
            db.flush()
            return existing, True

        record = PrayerHistory(
            user_id=user_id,
            prayer_date=payload.prayer_date,
            prayer=payload.prayer,
            status=payload.status,
            scheduled_at=payload.scheduled_at,
            window_ends_at=payload.window_ends_at,
            verification_deadline=payload.verification_deadline,
            qaza_deadline=payload.qaza_deadline,
            completed_at=payload.completed_at,
            qaza_completed_at=payload.qaza_completed_at,
            delay_minutes=payload.delay_minutes,
            excuse_reason=payload.excuse_reason,
        )
        db.add(record)
        db.flush()
        return record, False

    def record_verification(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        payload: VerificationRecordUpload,
    ) -> tuple[PrayerVerification, bool]:
        # The device sends its local prayer_history_id. Map it to the server
        # row via the deterministic (date, prayer) key the client also uses, so
        # a verification is linked even though the two sides mint different
        # primary keys. The client id format is "<date>:<prayer>".
        server_prayer = self._resolve_prayer_history(
            db, user_id=user_id, client_prayer_id=payload.prayer_history_id
        )
        if server_prayer is None:
            # The verification arrived before its prayer. The sync queue orders
            # by creation time, so this is rare, but if it happens the caller
            # should retry rather than lose the record.
            raise LookupError("prayer_history_not_found")

        existing = db.execute(
            select(PrayerVerification).where(
                PrayerVerification.user_id == user_id,
                PrayerVerification.prayer_history_id == server_prayer.id,
                PrayerVerification.attempt_number == payload.attempt_number,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True

        record = PrayerVerification(
            user_id=user_id,
            prayer_history_id=server_prayer.id,
            status=(
                VerificationStatus.APPROVED
                if payload.approved
                else VerificationStatus.REJECTED
            ),
            provider="device",
            provider_model="on-device",
            attempt_number=payload.attempt_number,
            rejection_reason=None if payload.approved else payload.message,
        )
        db.add(record)
        db.flush()
        return record, False

    def record_lock_session(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        payload: LockSessionUpload,
    ) -> tuple[LockSession, bool]:
        existing = db.execute(
            select(LockSession).where(
                LockSession.user_id == user_id,
                LockSession.prayer == payload.prayer,
                LockSession.started_at == payload.started_at,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Update the close fields, since a session may be uploaded once when
            # it ends with fuller data than any earlier partial.
            existing.ended_at = payload.ended_at
            existing.end_reason = payload.end_reason
            existing.interception_count = payload.interception_count
            db.flush()
            return existing, True

        record = LockSession(
            user_id=user_id,
            prayer=payload.prayer,
            started_at=payload.started_at,
            ended_at=payload.ended_at,
            end_reason=payload.end_reason,
            blocked_app_count=payload.blocked_app_count,
            interception_count=payload.interception_count,
            is_morning_protection=payload.is_morning_protection,
        )
        db.add(record)
        db.flush()
        return record, False

    def record_emergency_unlock(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        payload: EmergencyUnlockUpload,
    ) -> tuple[EmergencyUnlock, bool]:
        existing = db.execute(
            select(EmergencyUnlock).where(
                EmergencyUnlock.user_id == user_id,
                EmergencyUnlock.unlock_date == payload.unlock_date,
                EmergencyUnlock.daily_sequence == payload.daily_sequence,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True

        record = EmergencyUnlock(
            user_id=user_id,
            unlock_date=payload.unlock_date,
            daily_sequence=payload.daily_sequence,
            reason=payload.reason,
            idempotency_key=payload.client_id,
        )
        db.add(record)
        db.flush()

        record_audit_event(
            db,
            user_id=user_id,
            action=AuditAction.EMERGENCY_UNLOCK,
            detail={"unlock_date": payload.unlock_date.isoformat(),
                    "sequence": payload.daily_sequence},
        )
        return record, False

    # --- Statistics ------------------------------------------------------

    def statistics(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        today: date | None = None,
    ) -> DashboardStatistics:
        anchor = today or datetime.now(UTC).date()

        rows = db.execute(
            select(
                PrayerHistory.prayer_date,
                PrayerHistory.prayer,
                PrayerHistory.status,
            ).where(PrayerHistory.user_id == user_id)
        ).all()

        by_day: dict[date, _Counts] = defaultdict(_Counts)
        by_prayer: dict[PrayerName, _Counts] = defaultdict(_Counts)
        for prayer_date, prayer, status in rows:
            by_day[prayer_date].add(status)
            by_prayer[prayer].add(status)

        def sum_since(start: date) -> _Counts:
            total = _Counts()
            for day, counts in by_day.items():
                if day >= start:
                    total.completed += counts.completed
                    total.qaza += counts.qaza
                    total.missed += counts.missed
                    total.excused += counts.excused
            return total

        all_time = _Counts()
        for counts in by_day.values():
            all_time.completed += counts.completed
            all_time.qaza += counts.qaza
            all_time.missed += counts.missed
            all_time.excused += counts.excused

        streak = self._streak(
            [_DailySummary(day, counts) for day, counts in by_day.items()],
            anchor,
        )

        return DashboardStatistics(
            today=sum_since(anchor).to_schema(),
            week=sum_since(anchor - timedelta(days=6)).to_schema(),
            month=sum_since(anchor.replace(day=1)).to_schema(),
            year=sum_since(anchor.replace(month=1, day=1)).to_schema(),
            all_time=all_time.to_schema(),
            streak=streak,
            by_prayer={
                prayer: counts.to_schema() for prayer, counts in by_prayer.items()
            },
        )

    # --- Internals -------------------------------------------------------

    @staticmethod
    def _streak(summaries: list[_DailySummary], today: date) -> StreakResponse:
        perfect_days = {s.day for s in summaries if s.is_perfect}
        if not perfect_days:
            return StreakResponse(current=0, longest=0, last_perfect_day=None)

        ordered = sorted(perfect_days)

        # Longest run of consecutive days.
        longest = run = 1
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (current - previous).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1

        # Current run, anchored at today or yesterday. Yesterday counts because
        # today's prayers are not all done yet; requiring today complete would
        # show a zero streak every morning.
        yesterday = today - timedelta(days=1)
        if today in perfect_days:
            anchor = today
        elif yesterday in perfect_days:
            anchor = yesterday
        else:
            return StreakResponse(
                current=0, longest=longest, last_perfect_day=ordered[-1]
            )

        current = 0
        cursor = anchor
        while cursor in perfect_days:
            current += 1
            cursor -= timedelta(days=1)

        return StreakResponse(
            current=current,
            longest=max(longest, current),
            last_perfect_day=ordered[-1],
        )

    @staticmethod
    def _resolve_prayer_history(
        db: Session,
        *,
        user_id: uuid.UUID,
        client_prayer_id: str,
    ) -> PrayerHistory | None:
        # Client id is "YYYY-MM-DD:prayer".
        try:
            date_part, prayer_part = client_prayer_id.rsplit(":", 1)
            prayer_date = date.fromisoformat(date_part)
            prayer = PrayerName(prayer_part)
        except (ValueError, KeyError):
            return None

        return db.execute(
            select(PrayerHistory).where(
                PrayerHistory.user_id == user_id,
                PrayerHistory.prayer_date == prayer_date,
                PrayerHistory.prayer == prayer,
            )
        ).scalar_one_or_none()


tracking_service = TrackingService()
