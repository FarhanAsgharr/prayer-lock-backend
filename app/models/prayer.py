"""Computed prayer schedules and per-prayer completion history."""

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import CalculationMethod, Madhab, PrayerName, PrayerStatus

if TYPE_CHECKING:
    # Import only for type checking; at runtime SQLAlchemy resolves the
    # relationship by name from its registry, and importing eagerly here
    # would create a circular import with app.models.verification.
    from app.models.verification import PrayerVerification


class PrayerTimes(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One day's computed schedule for a user at a location.

    Times are computed on-device (the app must work fully offline) and mirrored
    here so the server can drive push reminders and analytics. The calculation
    inputs are stored alongside the results so a schedule can be recomputed and
    audited after a settings change.
    """

    __tablename__ = "prayer_times"
    __table_args__ = (
        UniqueConstraint("user_id", "prayer_date", name="uq_prayer_times_user_date"),
        Index("ix_prayer_times_user_date", "user_id", "prayer_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    prayer_date: Mapped[date] = mapped_column(Date, nullable=False)

    # All timestamps are timezone-aware UTC instants. Rendering into the user's
    # local wall-clock is a presentation concern, kept out of storage so that
    # DST transitions and travel across timezones cannot corrupt history.
    fajr_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sunrise_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dhuhr_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    asr_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    maghrib_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    isha_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Calculation inputs captured at computation time.
    calculation_method: Mapped[CalculationMethod] = mapped_column(
        pg_enum(CalculationMethod, "calculation_method"), nullable=False
    )
    madhab: Mapped[Madhab] = mapped_column(pg_enum(Madhab, "madhab"), nullable=False)


class PrayerHistory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The status of one prayer, on one day, for one user.

    This is the analytics spine: streaks, completion rates and monthly
    statistics are all aggregations over this table.
    """

    __tablename__ = "prayer_history"
    __table_args__ = (
        UniqueConstraint("user_id", "prayer_date", "prayer", name="uq_prayer_history_entry"),
        Index("ix_prayer_history_user_date", "user_id", "prayer_date"),
        Index("ix_prayer_history_user_status", "user_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    prayer_date: Mapped[date] = mapped_column(Date, nullable=False)
    prayer: Mapped[PrayerName] = mapped_column(pg_enum(PrayerName, "prayer_name"), nullable=False)
    status: Mapped[PrayerStatus] = mapped_column(
        pg_enum(PrayerStatus, "prayer_status"),
        default=PrayerStatus.PENDING,
        nullable=False,
    )

    # Scheduled instant, denormalised from PrayerTimes so history rows remain
    # meaningful even if the schedule is later recomputed or the location
    # deleted. History must be immutable once written.
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Effective end of everything actionable (the qaza deadline).
    window_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # End of the on-time window (scheduled_at + 30m) and of the qaza window
    # (scheduled_at + 90m). Nullable so rows migrated from before this feature
    # remain valid; recomputable from scheduled_at when absent.
    verification_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    qaza_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Verification timestamp within the on-time window.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Verification timestamp within the qaza window (distinct from on-time).
    qaza_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Minutes between scheduled_at and verification; negative is impossible.
    delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Set when the user marks a prayer excused rather than completing it.
    excuse_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    verifications: Mapped[list["PrayerVerification"]] = relationship(
        back_populates="prayer_history",
        cascade="all, delete-orphan",
    )
