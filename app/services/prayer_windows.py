"""Dynamic prayer-duration calculation.

A deliberate mirror of the client's
`lib/features/prayer_times/domain/usecases/dynamic_duration_calculator.dart`.
The duplication is the point: the app must compute windows with no network, so
the logic cannot live only here — but the server needs the same answer to build
schedules for push scheduling and for the analytics that explain to a user why
their phone was locked for three hours on a Tuesday.

If you change the rules here, change them there, and update both test suites in
the same commit.

The one genuinely hard part is ordering. The astronomical calculator can, at
high latitudes and under some high-latitude fallback rules, return times that
are not in ascending order: Isha pushed past the following Fajr in midsummer, or
Asr landing before Dhuhr. A window built naively from those would run backwards,
and a backwards window means a lock whose end is in the past — which either
never engages or never releases. So boundaries are forced monotonic first, and
anything that had to move is flagged rather than silently corrected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from enum import StrEnum

from app.models.enums import PrayerName
from app.services.prayer_times import PrayerSchedule


class WindowBoundary(StrEnum):
    """What closes a prayer's window, for display ("End: Sunrise")."""

    SUNRISE = "sunrise"
    DHUHR = "dhuhr"
    ASR = "asr"
    MAGHRIB = "maghrib"
    ISHA = "isha"
    NEXT_DAY_FAJR = "next_day_fajr"


#: The boundary that closes each prayer's window.
#:
#: Fixed by fiqh, not by configuration — hence a module constant rather than a
#: setting. Fajr ends at sunrise rather than running on to Dhuhr, because
#: praying Fajr after sunrise is qada, not adaa; treating the whole morning as
#: one Fajr window would both be wrong and would block apps for six hours.
WINDOW_BOUNDARIES: dict[PrayerName, WindowBoundary] = {
    PrayerName.FAJR: WindowBoundary.SUNRISE,
    PrayerName.DHUHR: WindowBoundary.ASR,
    PrayerName.ASR: WindowBoundary.MAGHRIB,
    PrayerName.MAGHRIB: WindowBoundary.ISHA,
    PrayerName.ISHA: WindowBoundary.NEXT_DAY_FAJR,
}


@dataclass(frozen=True, slots=True)
class PrayerWindow:
    """One prayer's computed window."""

    prayer: PrayerName
    starts_at: datetime
    ends_at: datetime
    boundary: WindowBoundary
    was_clamped: bool = False

    @property
    def duration(self) -> timedelta:
        """How long apps stay blocked for this prayer under the full-duration policy."""
        return self.ends_at - self.starts_at

    @property
    def duration_minutes(self) -> int:
        return int(self.duration.total_seconds() // 60)

    @property
    def is_empty(self) -> bool:
        """A window with no time in it. Nothing is ever owed during one."""
        return self.ends_at <= self.starts_at

    def contains(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at


@dataclass(frozen=True, slots=True)
class DailyPrayerWindows:
    """The five windows for one local calendar day."""

    prayer_date: date_type
    windows: tuple[PrayerWindow, ...]
    sunrise: datetime
    next_day_fajr: datetime

    def window_for(self, prayer: PrayerName) -> PrayerWindow:
        for window in self.windows:
            if window.prayer is prayer:
                return window
        raise KeyError(f"No window for {prayer}")

    def window_at(self, moment: datetime) -> PrayerWindow | None:
        """The window containing *moment*, or None when no prayer is due.

        None is the normal state between sunrise and Dhuhr.
        """
        for window in self.windows:
            if window.contains(moment):
                return window
        return None

    @property
    def total_duration(self) -> timedelta:
        """Total time the day's windows cover.

        Informational: shown in settings so a user can see what "block for the
        full duration" costs before switching to it.
        """
        return sum((window.duration for window in self.windows), timedelta())

    @property
    def has_clamped_windows(self) -> bool:
        return any(window.was_clamped for window in self.windows)


def _force_ascending(instants: list[datetime]) -> list[datetime]:
    """Raise each instant to at least its predecessor, preserving order.

    Clamping upward rather than downward is deliberate: pulling a boundary
    *earlier* would end a window before the prayer it belongs to had begun,
    whereas raising it merely collapses the offending window to zero length,
    which the lock logic already treats as "nothing owed".
    """
    result = [instants[0]]
    for current in instants[1:]:
        previous = result[-1]
        result.append(previous if current < previous else current)
    return result


def _is_ascending(instants: list[datetime]) -> bool:
    return all(later >= earlier for earlier, later in zip(instants, instants[1:], strict=False))


def compute_windows(
    schedule: PrayerSchedule,
    next_day_fajr: datetime,
) -> DailyPrayerWindows:
    """Build a day's windows from *schedule* and the following day's Fajr.

    *next_day_fajr* is required rather than optional: Isha's window has no
    defined end without it, and defaulting to midnight or to a fixed number of
    hours would reintroduce exactly the hardcoded duration this replaces.
    """
    raw = [
        schedule.fajr,
        schedule.sunrise,
        schedule.dhuhr,
        schedule.asr,
        schedule.maghrib,
        schedule.isha,
        next_day_fajr,
    ]

    ordered = _force_ascending(raw)
    fajr, sunrise, dhuhr, asr, maghrib, isha, following_fajr = ordered

    # True when any boundary had to move. Applied to every window rather than
    # tracked per index, because a single displaced boundary distorts the two
    # windows either side of it and there is no honest way to say only one of
    # them is affected.
    clamped = not _is_ascending(raw)

    def window(prayer: PrayerName, start: datetime, end: datetime) -> PrayerWindow:
        return PrayerWindow(
            prayer=prayer,
            starts_at=start,
            ends_at=end,
            boundary=WINDOW_BOUNDARIES[prayer],
            was_clamped=clamped,
        )

    return DailyPrayerWindows(
        prayer_date=schedule.prayer_date,
        sunrise=sunrise,
        next_day_fajr=following_fajr,
        windows=(
            # Fajr ends at sunrise, not at Dhuhr. See WINDOW_BOUNDARIES.
            window(PrayerName.FAJR, fajr, sunrise),
            window(PrayerName.DHUHR, dhuhr, asr),
            window(PrayerName.ASR, asr, maghrib),
            window(PrayerName.MAGHRIB, maghrib, isha),
            window(PrayerName.ISHA, isha, following_fajr),
        ),
    )


def format_duration(duration: timedelta) -> str:
    """Human-readable duration, e.g. "3 hours 37 minutes"."""
    total_minutes = int(duration.total_seconds() // 60)
    if total_minutes <= 0:
        return "0 minutes"

    hours, minutes = divmod(total_minutes, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours} {'hour' if hours == 1 else 'hours'}")
    if minutes:
        parts.append(f"{minutes} {'minute' if minutes == 1 else 'minutes'}")

    return " ".join(parts)
