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
