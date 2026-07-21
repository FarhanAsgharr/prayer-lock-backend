"""App-blocking configuration, lock sessions and emergency unlocks."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import PlatformType, PrayerName


class BlockedAppCatalogEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Admin-curated catalogue of commonly-blocked apps.

    Seeds the suggestion list in the app's picker. Users may block anything
    installed on their device; this table exists so the suggested defaults can
    be updated remotely without shipping a new release.
    """

    __tablename__ = "blocked_app_catalog"
    __table_args__ = (
        UniqueConstraint("platform", "package_identifier", name="uq_catalog_platform_package"),
    )

    platform: Mapped[PlatformType] = mapped_column(
        pg_enum(PlatformType, "platform_type"), nullable=False
    )
    # Android package name, or iOS bundle identifier.
    package_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    # Suggested on by default for new users.
    is_default_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserBlockedApp(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An app a specific user has chosen to restrict during prayer."""

    __tablename__ = "user_blocked_apps"
    __table_args__ = (
        UniqueConstraint("user_id", "package_identifier", name="uq_user_blocked_package"),
        Index("ix_user_blocked_apps_user_enabled", "user_id", "is_enabled"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LockSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One period during which apps were restricted.

    Opened when Prayer Mode engages, closed on verification, emergency unlock,
    or window expiry. The device is authoritative for lock state while offline;
    these rows are the synced record used for history and analytics.
    """

    __tablename__ = "lock_sessions"
    __table_args__ = (
        Index("ix_lock_sessions_user_started", "user_id", "started_at"),
        Index("ix_lock_sessions_open", "user_id", "ended_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    prayer_history_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("prayer_history.id", ondelete="SET NULL"),
        nullable=True,
    )
    prayer: Mapped[PrayerName] = mapped_column(pg_enum(PrayerName, "prayer_name"), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL means the session is still open.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "verified" | "emergency_unlock" | "window_expired" | "user_disabled"
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    blocked_app_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How many times the user opened a blocked app during the lock.
    interception_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # True for the post-Fajr "Good Morning" gate rather than a normal lock.
    is_morning_protection: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EmergencyUnlock(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A user-initiated bypass of an active lock.

    Rate-limited per day by UserSettings.max_emergency_unlocks_per_day. The
    daily quota is enforced with a partial unique index on (user_id,
    unlock_date, daily_sequence) so that a client retrying a request during a
    network partition cannot consume two unlocks.
    """

    __tablename__ = "emergency_unlocks"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "unlock_date", "daily_sequence", name="uq_emergency_unlock_daily"
        ),
        Index("ix_emergency_unlocks_user_date", "user_id", "unlock_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    lock_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("lock_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Local calendar date in the user's timezone, not UTC — the quota is
    # "one per day" as the user experiences days.
    unlock_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 1 for the first unlock of the day, 2 for the second, etc.
    daily_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Client-supplied idempotency key, so a retried request is a no-op.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
