"""User identity, device registration, location and per-user settings."""

import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import (
    CalculationMethod,
    HighLatitudeRule,
    Madhab,
    PlatformType,
)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    # Firebase is the identity provider; we never store passwords.
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    settings: Mapped["UserSettings"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    locations: Mapped[list["Location"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    devices: Mapped[list["Device"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Device(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A registered installation. Push tokens are per-device, not per-user."""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("user_id", "install_id", name="uq_devices_user_install"),
        Index("ix_devices_fcm_token", "fcm_token"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Client-generated stable identifier surviving app restarts but not reinstalls.
    install_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[PlatformType] = mapped_column(
        pg_enum(PlatformType, "platform_type"), nullable=False
    )
    os_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fcm_token: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Android: whether the user has actually granted the special permissions.
    # Blocking silently no-ops without these, so we track them for support.
    has_usage_stats_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    has_overlay_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    has_notification_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    # iOS: whether the Family Controls authorization was granted.
    has_family_controls_authorization: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="devices")


class Location(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A prayer-time computation origin — auto-detected or manually chosen."""

    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="latitude_range"),
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="longitude_range"),
        Index("ix_locations_user_active", "user_id", "is_active"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_meters: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    # False when the user picked the location by hand on the map.
    is_auto_detected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Exactly one active location per user drives the schedule.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="locations")


class UserSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-user prayer, notification and enforcement preferences."""

    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            "reminder_minutes_before >= 0 AND reminder_minutes_before <= 120",
            name="reminder_window",
        ),
        CheckConstraint(
            "lock_grace_period_minutes >= 0 AND lock_grace_period_minutes <= 60",
            name="grace_period_range",
        ),
        CheckConstraint(
            "max_emergency_unlocks_per_day >= 0 AND max_emergency_unlocks_per_day <= 5",
            name="emergency_unlock_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # --- Prayer calculation ---------------------------------------------
    calculation_method: Mapped[CalculationMethod] = mapped_column(
        pg_enum(CalculationMethod, "calculation_method"),
        default=CalculationMethod.MUSLIM_WORLD_LEAGUE,
        nullable=False,
    )
    madhab: Mapped[Madhab] = mapped_column(
        pg_enum(Madhab, "madhab"), default=Madhab.SHAFI, nullable=False
    )
    high_latitude_rule: Mapped[HighLatitudeRule] = mapped_column(
        pg_enum(HighLatitudeRule, "high_latitude_rule"),
        default=HighLatitudeRule.MIDDLE_OF_THE_NIGHT,
        nullable=False,
    )
    # Per-prayer manual corrections in minutes, matching local mosque practice.
    fajr_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dhuhr_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    asr_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    maghrib_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    isha_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Notifications ---------------------------------------------------
    reminder_minutes_before: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    adhan_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    adhan_sound: Mapped[str] = mapped_column(String(64), default="makkah", nullable=False)

    # --- Enforcement -----------------------------------------------------
    blocking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Delay between adhan and lock engaging, so the user can finish a call.
    lock_grace_period_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # The "Good Morning, pray Fajr first" gate described in the product spec.
    morning_protection_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_ai_verification: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_emergency_unlocks_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Optional nightly window during which blocking never engages.
    quiet_hours_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time, nullable=True)

    user: Mapped[User] = relationship(back_populates="settings")
