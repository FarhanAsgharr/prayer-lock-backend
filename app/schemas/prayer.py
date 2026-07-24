"""Prayer schedule and verification request/response bodies."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import (
    CalculationMethod,
    HighLatitudeRule,
    Madhab,
    PrayerName,
    VerificationStatus,
)


class PrayerTimesQuery(BaseModel):
    """Explicit calculation inputs.

    Latitude and longitude are optional: when omitted the user's active
    location is used. Supplying them lets the client preview a schedule for a
    place the user is considering, without mutating their saved location.
    """

    prayer_date: date | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = Field(default=None, max_length=64)
    method: CalculationMethod | None = None
    madhab: Madhab | None = None
    high_latitude_rule: HighLatitudeRule | None = None


class PrayerTimesResponse(BaseModel):
    prayer_date: date
    timezone: str
    method: CalculationMethod
    madhab: Madhab
    fajr: datetime
    sunrise: datetime
    dhuhr: datetime
    asr: datetime
    maghrib: datetime
    isha: datetime
    next_prayer: PrayerName | None = None
    next_prayer_at: datetime | None = None
    seconds_until_next_prayer: int | None = None


class PrayerWindowSchema(BaseModel):
    """One prayer's computed blocking window."""

    prayer: PrayerName
    starts_at: datetime
    ends_at: datetime
    #: What closes the window — "sunrise", "asr", "next_day_fajr" and so on.
    #: Sent so the client can render "End: Sunrise" without duplicating the
    #: fiqh rules that decide it.
    boundary: str
    duration_minutes: int
    #: Preformatted on the server so the same wording appears in a push
    #: notification and in the app.
    duration_label: str


class PrayerScheduleResponse(BaseModel):
    """A day's prayer times together with the durations derived from them."""

    prayer_date: date
    timezone: str
    method: CalculationMethod
    madhab: Madhab

    fajr: datetime
    sunrise: datetime
    dhuhr: datetime
    asr: datetime
    maghrib: datetime
    isha: datetime

    #: The following day's Fajr, which closes the Isha window. Included so a
    #: client can reconstruct every window from this response alone.
    next_day_fajr: datetime

    windows: list[PrayerWindowSchema]
    total_blocked_minutes: int

    #: True when the raw astronomical times were out of order and had to be
    #: clamped — which happens at extreme latitudes. Surfaced rather than
    #: hidden so a client can warn instead of showing a nonsensical duration.
    has_clamped_windows: bool = False


class PrayerScheduleRangeResponse(BaseModel):
    """Consecutive days, for prefetching an offline cache."""

    days: list[PrayerScheduleResponse]


class VerificationRequest(BaseModel):
    prayer_history_id: uuid.UUID
    image_base64: str = Field(
        min_length=64,
        # Roughly 6 MB of base64, about a 4.5 MB image. Larger payloads are
        # rejected at the edge rather than forwarded to the vision provider.
        max_length=8_000_000,
        description="Base64-encoded JPEG or PNG capture",
    )


class VerificationResponse(BaseModel):
    verification_id: uuid.UUID
    status: VerificationStatus
    approved: bool
    attempt_number: int
    attempts_remaining: int
    message: str
    is_suspected_replay: bool
