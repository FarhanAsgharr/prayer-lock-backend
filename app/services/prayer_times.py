"""Astronomical prayer-time calculation.

Implements the standard solar-position algorithm used by the major timetable
authorities. Everything here is pure computation with no I/O, which is what
allows the identical logic to run on-device with no network — the offline
requirement in the spec is met by design, not by caching.

The mobile client carries a Dart port of this module. Both are validated
against the same fixture set so the two implementations cannot silently
diverge.

Reference: the solar position formulae follow the U.S. Naval Observatory's
low-precision approximation, accurate to well under a minute for prayer-time
purposes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.enums import CalculationMethod, HighLatitudeRule, Madhab, PrayerName

# Sun altitude at sunrise/sunset: the solar disc's upper limb touching the
# horizon, including atmospheric refraction.
_HORIZON_ANGLE = 0.833


@dataclass(frozen=True)
class MethodParameters:
    """Solar-depression angles defining Fajr and Isha for one authority.

    Some authorities define Isha as a fixed interval after Maghrib rather than
    a sun angle, because at their latitudes the angle-based definition is
    unstable in summer.
    """

    fajr_angle: float
    isha_angle: float | None = None
    isha_interval_minutes: int | None = None
    maghrib_angle: float = _HORIZON_ANGLE

    def __post_init__(self) -> None:
        if (self.isha_angle is None) == (self.isha_interval_minutes is None):
            raise ValueError("Exactly one of isha_angle or isha_interval_minutes must be set")


# Angles as published by each authority.
METHOD_PARAMETERS: dict[CalculationMethod, MethodParameters] = {
    CalculationMethod.MUSLIM_WORLD_LEAGUE: MethodParameters(fajr_angle=18.0, isha_angle=17.0),
    CalculationMethod.EGYPTIAN: MethodParameters(fajr_angle=19.5, isha_angle=17.5),
    CalculationMethod.KARACHI: MethodParameters(fajr_angle=18.0, isha_angle=18.0),
    # Umm al-Qura fixes Isha at 90 minutes after Maghrib (120 during Ramadan).
    CalculationMethod.UMM_AL_QURA: MethodParameters(fajr_angle=18.5, isha_interval_minutes=90),
    CalculationMethod.DUBAI: MethodParameters(fajr_angle=18.2, isha_angle=18.2),
    CalculationMethod.QATAR: MethodParameters(fajr_angle=18.0, isha_interval_minutes=90),
    CalculationMethod.KUWAIT: MethodParameters(fajr_angle=18.0, isha_angle=17.5),
    CalculationMethod.MOONSIGHTING_COMMITTEE: MethodParameters(fajr_angle=18.0, isha_angle=18.0),
    CalculationMethod.SINGAPORE: MethodParameters(fajr_angle=20.0, isha_angle=18.0),
    CalculationMethod.TURKEY: MethodParameters(fajr_angle=18.0, isha_angle=17.0),
    # Tehran also lifts Maghrib off the horizon to a 4.5 degree depression.
    CalculationMethod.TEHRAN: MethodParameters(fajr_angle=17.7, isha_angle=14.0, maghrib_angle=4.5),
    CalculationMethod.NORTH_AMERICA: MethodParameters(fajr_angle=15.0, isha_angle=15.0),
}

# Asr begins when an object's shadow equals its own length plus this multiple.
# Asr begins when an object's shadow equals its own length plus this multiple.
# Every school except Hanafi uses ratio 1; Hanafi uses 2.
_MADHAB_SHADOW_FACTOR: dict[Madhab, int] = {
    Madhab.SHAFI: 1,
    Madhab.HANAFI: 2,
    Madhab.AHLE_HADITH: 1,
    Madhab.JAFARI: 1,
}

# Schools that define Maghrib by a sun-depression angle rather than at sunset.
# The Ja'fari (Shia) ruling is that Maghrib begins when the redness in the
# eastern sky disappears, conventionally approximated as the sun 4 degrees
# below the horizon — noticeably later than sunset.
_MADHAB_MAGHRIB_ANGLE: dict[Madhab, float] = {
    Madhab.JAFARI: 4.0,
}


@dataclass(frozen=True)
class SolarPosition:
    declination: float  # degrees
    equation_of_time: float  # hours


def julian_day(target: date) -> float:
    """Julian Day Number at 00:00 UT on the given civil date."""
    year, month, day = target.year, target.month, target.day
    if month <= 2:
        year -= 1
        month += 12

    a = math.floor(year / 100)
    # Gregorian calendar correction.
    b = 2 - a + math.floor(a / 4)

    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def solar_position(jd: float) -> SolarPosition:
    """Sun declination and equation of time for a Julian Day."""
    d = jd - 2451545.0  # days since the J2000.0 epoch

    # Mean anomaly, mean longitude, and ecliptic longitude of the Sun.
    mean_anomaly = math.radians((357.529 + 0.98560028 * d) % 360)
    mean_longitude = (280.459 + 0.98564736 * d) % 360
    ecliptic_longitude = math.radians(
        (mean_longitude + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly)) % 360
    )

    obliquity = math.radians(23.439 - 0.00000036 * d)

    declination = math.degrees(math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude)))

    right_ascension = math.degrees(
        math.atan2(
            math.cos(obliquity) * math.sin(ecliptic_longitude),
            math.cos(ecliptic_longitude),
        )
    )
    right_ascension_hours = (right_ascension % 360) / 15.0

    equation_of_time = mean_longitude / 15.0 - right_ascension_hours
    # Fold into [-12, 12) hours; the raw difference can wrap a full day.
    equation_of_time = (equation_of_time + 12) % 24 - 12

    return SolarPosition(declination=declination, equation_of_time=equation_of_time)


def _hour_angle(sun_altitude: float, latitude: float, declination: float) -> float | None:
    """Hours between solar noon and the moment the sun reaches `sun_altitude`.

    Returns None when the sun never reaches that altitude on this date at this
    latitude — the high-latitude case that must be handled by a fallback rule
    rather than allowed to produce a nonsensical time.
    """
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    numerator = -math.sin(math.radians(sun_altitude)) - math.sin(dec_rad) * math.sin(lat_rad)
    denominator = math.cos(dec_rad) * math.cos(lat_rad)

    if denominator == 0:
        return None

    cosine = numerator / denominator
    if not -1.0 <= cosine <= 1.0:
        return None

    return math.degrees(math.acos(cosine)) / 15.0


def _asr_altitude(latitude: float, declination: float, shadow_factor: int) -> float:
    """Sun altitude at which the Asr shadow condition is satisfied."""
    # Negative because the angle is measured as a depression in _hour_angle,
    # while the sun is still above the horizon at Asr.
    return -math.degrees(
        math.atan(1.0 / (shadow_factor + math.tan(math.radians(abs(latitude - declination)))))
    )


@dataclass(frozen=True)
class PrayerSchedule:
    """One day's prayer instants, as timezone-aware UTC datetimes."""

    prayer_date: date
    fajr: datetime
    sunrise: datetime
    dhuhr: datetime
    asr: datetime
    maghrib: datetime
    isha: datetime

    def as_dict(self) -> dict[PrayerName, datetime]:
        return {
            PrayerName.FAJR: self.fajr,
            PrayerName.DHUHR: self.dhuhr,
            PrayerName.ASR: self.asr,
            PrayerName.MAGHRIB: self.maghrib,
            PrayerName.ISHA: self.isha,
        }

    def next_prayer_after(self, moment: datetime) -> tuple[PrayerName, datetime] | None:
        """The first prayer strictly after `moment`, or None if the day is done."""
        for name, when in sorted(self.as_dict().items(), key=lambda item: item[1]):
            if when > moment:
                return name, when
        return None


@dataclass(frozen=True)
class CalculationRequest:
    latitude: float
    longitude: float
    timezone: str
    prayer_date: date
    method: CalculationMethod = CalculationMethod.MUSLIM_WORLD_LEAGUE
    madhab: Madhab = Madhab.SHAFI
    high_latitude_rule: HighLatitudeRule = HighLatitudeRule.MIDDLE_OF_THE_NIGHT
    # Per-prayer manual corrections matching local mosque practice.
    adjustments: dict[PrayerName, int] | None = None

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"longitude out of range: {self.longitude}")


class PrayerTimeCalculator:
    """Computes a day's prayer schedule for a location and configuration."""

    def calculate(self, request: CalculationRequest) -> PrayerSchedule:
        tz = ZoneInfo(request.timezone)
        params = METHOD_PARAMETERS[request.method]
        shadow_factor = _MADHAB_SHADOW_FACTOR[request.madhab]

        # Evaluate the sun at local solar noon rather than at midnight; the
        # declination drifts measurably across a day and noon is the midpoint
        # of the interval that matters.
        jd = julian_day(request.prayer_date) - request.longitude / (15.0 * 24.0)
        sun = solar_position(jd + 0.5)

        # Local mean solar noon, expressed in hours of local standard time.
        utc_offset_hours = self._utc_offset_hours(tz, request.prayer_date)
        dhuhr_hours = 12.0 + utc_offset_hours - request.longitude / 15.0 - sun.equation_of_time

        # Maghrib angle: take the later of the method's convention and the
        # school's. The Ja'fari school delays Maghrib to 4 degrees; a method
        # like Tehran already specifies 4.5. Using the max means Maghrib is
        # never earlier than either the method or the school requires, and the
        # two never contradict each other.
        maghrib_angle = max(
            params.maghrib_angle,
            _MADHAB_MAGHRIB_ANGLE.get(request.madhab, 0.0),
        )

        sunrise_offset = _hour_angle(_HORIZON_ANGLE, request.latitude, sun.declination)
        maghrib_offset = _hour_angle(maghrib_angle, request.latitude, sun.declination)
        fajr_offset = _hour_angle(params.fajr_angle, request.latitude, sun.declination)
        asr_offset = _hour_angle(
            _asr_altitude(request.latitude, sun.declination, shadow_factor),
            request.latitude,
            sun.declination,
        )

        # Sunrise and sunset failing means polar day or night. There is no
        # meaningful astronomical answer, so we fall back to the nearest
        # latitude at which one exists rather than emitting a wrong time.
        if sunrise_offset is None or maghrib_offset is None:
            return self._polar_fallback(request, tz)

        sunrise_hours = dhuhr_hours - sunrise_offset
        maghrib_hours = dhuhr_hours + maghrib_offset
        asr_hours = dhuhr_hours + (asr_offset if asr_offset is not None else sunrise_offset)

        if params.isha_interval_minutes is not None:
            isha_hours = maghrib_hours + params.isha_interval_minutes / 60.0
        else:
            assert params.isha_angle is not None  # guaranteed by MethodParameters
            isha_offset = _hour_angle(params.isha_angle, request.latitude, sun.declination)
            isha_hours = dhuhr_hours + isha_offset if isha_offset is not None else float("nan")

        fajr_hours = dhuhr_hours - fajr_offset if fajr_offset is not None else float("nan")

        # Night length drives every high-latitude fallback rule.
        night_length = (sunrise_hours + 24.0) - maghrib_hours

        fajr_hours = self._resolve_high_latitude(
            value=fajr_hours,
            boundary=sunrise_hours,
            night_length=night_length,
            angle=params.fajr_angle,
            rule=request.high_latitude_rule,
            is_before_boundary=True,
        )
        isha_hours = self._resolve_high_latitude(
            value=isha_hours,
            boundary=maghrib_hours,
            night_length=night_length,
            angle=params.isha_angle or 18.0,
            rule=request.high_latitude_rule,
            is_before_boundary=False,
        )

        adjustments = request.adjustments or {}
        to_utc = self._hours_to_utc

        return PrayerSchedule(
            prayer_date=request.prayer_date,
            fajr=to_utc(fajr_hours, request, tz, adjustments.get(PrayerName.FAJR, 0)),
            sunrise=to_utc(sunrise_hours, request, tz, 0),
            dhuhr=to_utc(dhuhr_hours, request, tz, adjustments.get(PrayerName.DHUHR, 0)),
            asr=to_utc(asr_hours, request, tz, adjustments.get(PrayerName.ASR, 0)),
            maghrib=to_utc(maghrib_hours, request, tz, adjustments.get(PrayerName.MAGHRIB, 0)),
            isha=to_utc(isha_hours, request, tz, adjustments.get(PrayerName.ISHA, 0)),
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _utc_offset_hours(tz: ZoneInfo, target: date) -> float:
        """UTC offset in hours, evaluated at local noon on the given date.

        Noon is deliberate: evaluating at midnight would pick the wrong side of
        a DST transition that occurs in the early hours, which is exactly when
        most transitions happen.
        """
        reference = datetime(target.year, target.month, target.day, 12, 0, tzinfo=tz)
        offset = reference.utcoffset()
        return offset.total_seconds() / 3600.0 if offset else 0.0

    @staticmethod
    def _resolve_high_latitude(
        value: float,
        boundary: float,
        night_length: float,
        angle: float,
        rule: HighLatitudeRule,
        is_before_boundary: bool,
    ) -> float:
        """Clamp Fajr/Isha when the geometric time is absent or implausible.

        Above roughly 48 degrees the sun may never descend far enough for the
        angle-based definition to yield a time at all, or it may yield one so
        close to sunrise that following it is impractical. Each authority
        prescribes a different fallback; all of them cap the time at some
        fraction of the night.
        """
        if rule is HighLatitudeRule.MIDDLE_OF_THE_NIGHT:
            portion = night_length / 2.0
        elif rule is HighLatitudeRule.SEVENTH_OF_THE_NIGHT:
            portion = night_length / 7.0
        else:  # TWILIGHT_ANGLE
            portion = night_length * (angle / 60.0)

        limit = boundary - portion if is_before_boundary else boundary + portion

        if math.isnan(value):
            return limit

        # Keep whichever time is closer to the boundary — never allow Fajr
        # earlier, or Isha later, than the rule permits.
        return max(value, limit) if is_before_boundary else min(value, limit)

    def _polar_fallback(self, request: CalculationRequest, tz: ZoneInfo) -> PrayerSchedule:
        """Recompute at 48 degrees latitude during polar day or night.

        No sunrise or sunset exists to anchor the calculation, so we substitute
        the nearest latitude where the normal rules hold. This matches the
        "nearest latitude" convention used by high-latitude communities, and is
        preferable to returning nothing when the app must still show a schedule.
        """
        substitute_latitude = 48.0 if request.latitude >= 0 else -48.0
        fallback = CalculationRequest(
            latitude=substitute_latitude,
            longitude=request.longitude,
            timezone=request.timezone,
            prayer_date=request.prayer_date,
            method=request.method,
            madhab=request.madhab,
            high_latitude_rule=request.high_latitude_rule,
            adjustments=request.adjustments,
        )
        return self.calculate(fallback)

    @staticmethod
    def _hours_to_utc(
        hours: float,
        request: CalculationRequest,
        tz: ZoneInfo,
        adjustment_minutes: int,
    ) -> datetime:
        """Convert local-standard-time hours into an aware UTC instant."""
        base = datetime(
            request.prayer_date.year,
            request.prayer_date.month,
            request.prayer_date.day,
            tzinfo=tz,
        )
        # `hours` may fall outside [0, 24) — Isha after midnight, or Fajr
        # pushed before it — so timedelta arithmetic carries the date correctly.
        local = base + timedelta(hours=hours, minutes=adjustment_minutes)
        return local.astimezone(ZoneInfo("UTC"))


calculator = PrayerTimeCalculator()
