"""Request and response bodies for prayer tracking, sessions and analytics.

Every create endpoint accepts a `client_id`: the mobile app generates records
offline and assigns them stable ids, so the same record retried after a flaky
upload must be recognised as a duplicate rather than inserted twice. The
`client_id` is what makes that idempotency possible.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import PlatformType, PrayerName, PrayerStatus
from app.schemas.common import ORMModel


# --- Device registration -------------------------------------------------
class DeviceRegistrationRequest(BaseModel):
    install_id: str = Field(min_length=8, max_length=128)
    platform: PlatformType
    os_version: str | None = Field(default=None, max_length=32)
    app_version: str | None = Field(default=None, max_length=32)
    fcm_token: str | None = Field(default=None, max_length=512)
    # Android special-permission state, reported so support can diagnose a user
    # whose blocking silently does nothing.
    has_usage_stats_permission: bool = False
    has_overlay_permission: bool = False
    has_notification_permission: bool = False
    has_family_controls_authorization: bool = False


class DeviceResponse(ORMModel):
    id: uuid.UUID
    install_id: str
    platform: PlatformType


# --- Prayer history ------------------------------------------------------
class PrayerHistoryUpload(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    prayer_date: date
    prayer: PrayerName
    status: PrayerStatus
    scheduled_at: datetime
    window_ends_at: datetime
    completed_at: datetime | None = None
    delay_minutes: int | None = Field(default=None, ge=0)
    excuse_reason: str | None = Field(default=None, max_length=256)


class PrayerHistoryResponse(BaseModel):
    id: uuid.UUID
    prayer_date: date
    prayer: PrayerName
    status: PrayerStatus
    delay_minutes: int | None


# --- Verification record -------------------------------------------------
class VerificationRecordUpload(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    # The device references its own local prayer-history row; the server maps
    # it to the synced server-side row.
    prayer_history_id: str = Field(min_length=1, max_length=128)
    approved: bool
    attempt_number: int = Field(ge=1)
    released_without_detection: bool = False
    message: str | None = Field(default=None, max_length=512)
    created_at: datetime


# --- Lock session --------------------------------------------------------
class LockSessionUpload(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    prayer: PrayerName
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: str | None = Field(default=None, max_length=32)
    blocked_app_count: int = Field(default=0, ge=0)
    interception_count: int = Field(default=0, ge=0)
    is_morning_protection: bool = False


# --- Emergency unlock ----------------------------------------------------
class EmergencyUnlockUpload(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    unlock_date: date
    daily_sequence: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=256)
    created_at: datetime


# --- Generic upload acknowledgement --------------------------------------
class UploadAck(BaseModel):
    """Uniform response to any record upload.

    `duplicate` tells the client the server already had this record, which is a
    success from the client's point of view — it can drop the item from its
    queue either way.
    """

    id: uuid.UUID
    client_id: str
    duplicate: bool = False


# --- Statistics ----------------------------------------------------------
class PeriodCounts(BaseModel):
    completed: int = 0
    late: int = 0
    missed: int = 0
    excused: int = 0
    # Completed + late, over completed + late + missed. Excused is excluded so
    # the rate is not inflated by exemptions.
    success_rate: float = 0.0


class StreakResponse(BaseModel):
    current: int
    longest: int
    last_perfect_day: date | None = None


class DashboardStatistics(BaseModel):
    today: PeriodCounts
    week: PeriodCounts
    month: PeriodCounts
    year: PeriodCounts
    all_time: PeriodCounts
    streak: StreakResponse
    by_prayer: dict[PrayerName, PeriodCounts]
