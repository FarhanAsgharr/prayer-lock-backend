"""Tests for dynamic prayer-duration calculation.

The rules mirrored here also live in the Flutter client
(`dynamic_duration_calculator.dart`). These assertions are written to match that
suite case for case, so a change made on one side and not the other shows up as
a failing test rather than as two implementations quietly disagreeing about how
long a phone should stay locked.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.enums import CalculationMethod, Madhab, PrayerName
from app.services.prayer_times import CalculationRequest, PrayerSchedule, calculator
from app.services.prayer_windows import (
    WindowBoundary,
    compute_windows,
    format_duration,
)

MAKKAH = (21.4225, 39.8262, "Asia/Riyadh")
LONDON = (51.5074, -0.1278, "Europe/London")
TROMSO = (69.6492, 18.9553, "Europe/Oslo")


def schedule_for(
    coordinates: tuple[float, float, str],
    prayer_date: date,
    method: CalculationMethod = CalculationMethod.MUSLIM_WORLD_LEAGUE,
    madhab: Madhab = Madhab.SHAFI,
) -> PrayerSchedule:
    latitude, longitude, timezone = coordinates
    return calculator.calculate(
        CalculationRequest(
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            prayer_date=prayer_date,
            method=method,
            madhab=madhab,
        )
    )


def windows_for(
    coordinates: tuple[float, float, str] = MAKKAH,
    prayer_date: date = date(2026, 7, 20),
):
    """The day's windows, resolving the following Fajr properly.

    Isha's duration depends on it, so approximating it would make every Isha
    assertion meaningless.
    """
    today = schedule_for(coordinates, prayer_date)
    tomorrow = schedule_for(coordinates, prayer_date + timedelta(days=1))
    return compute_windows(today, tomorrow.fajr)


class TestBoundaries:
    def test_fajr_ends_at_sunrise(self) -> None:
        daily = windows_for()
        fajr = daily.window_for(PrayerName.FAJR)
        assert fajr.ends_at == daily.sunrise
        assert fajr.boundary is WindowBoundary.SUNRISE

    def test_dhuhr_ends_when_asr_begins(self) -> None:
        daily = windows_for()
        assert (
            daily.window_for(PrayerName.DHUHR).ends_at
            == daily.window_for(PrayerName.ASR).starts_at
        )

    def test_asr_ends_when_maghrib_begins(self) -> None:
        daily = windows_for()
        assert (
            daily.window_for(PrayerName.ASR).ends_at
            == daily.window_for(PrayerName.MAGHRIB).starts_at
        )

    def test_maghrib_ends_when_isha_begins(self) -> None:
        daily = windows_for()
        assert (
            daily.window_for(PrayerName.MAGHRIB).ends_at
            == daily.window_for(PrayerName.ISHA).starts_at
        )

    def test_isha_ends_at_the_following_fajr(self) -> None:
        daily = windows_for()
        isha = daily.window_for(PrayerName.ISHA)
        assert isha.ends_at == daily.next_day_fajr
        assert isha.boundary is WindowBoundary.NEXT_DAY_FAJR

    def test_fajr_does_not_run_on_to_dhuhr(self) -> None:
        # The morning gap between sunrise and Dhuhr belongs to no prayer. If
        # Fajr ran to Dhuhr, apps would be blocked for most of the morning.
        daily = windows_for()
        assert (
            daily.window_for(PrayerName.FAJR).ends_at
            < daily.window_for(PrayerName.DHUHR).starts_at
        )


class TestDurations:
    def test_all_windows_are_positive(self) -> None:
        for window in windows_for().windows:
            assert window.duration > timedelta(), f"{window.prayer} is non-positive"

    def test_durations_are_not_uniform(self) -> None:
        # The defining property of the feature. If these were all equal the
        # windows would be fixed, whatever the code claims.
        durations = {window.duration_minutes for window in windows_for().windows}
        assert len(durations) > 1

    def test_durations_change_across_the_year(self) -> None:
        summer = windows_for(prayer_date=date(2026, 7, 20))
        winter = windows_for(prayer_date=date(2026, 1, 20))
        assert (
            summer.window_for(PrayerName.FAJR).duration
            != winter.window_for(PrayerName.FAJR).duration
        )

    def test_durations_change_by_latitude(self) -> None:
        makkah = windows_for(MAKKAH, date(2026, 6, 21)).window_for(PrayerName.ISHA).duration
        london = windows_for(LONDON, date(2026, 6, 21)).window_for(PrayerName.ISHA).duration
        assert makkah != london

    def test_total_is_the_sum_of_the_windows(self) -> None:
        daily = windows_for()
        assert daily.total_duration == sum(
            (window.duration for window in daily.windows), timedelta()
        )

    def test_windows_do_not_overlap(self) -> None:
        ordered = sorted(windows_for().windows, key=lambda window: window.starts_at)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            assert later.starts_at >= earlier.ends_at


class TestQueries:
    def test_contains_is_half_open(self) -> None:
        dhuhr = windows_for().window_for(PrayerName.DHUHR)
        assert dhuhr.contains(dhuhr.starts_at)
        assert not dhuhr.contains(dhuhr.ends_at)

    def test_window_at_finds_the_open_window(self) -> None:
        daily = windows_for()
        dhuhr = daily.window_for(PrayerName.DHUHR)
        found = daily.window_at(dhuhr.starts_at + timedelta(minutes=30))
        assert found is not None
        assert found.prayer is PrayerName.DHUHR

    def test_nothing_is_due_in_the_morning_gap(self) -> None:
        daily = windows_for()
        assert daily.window_at(daily.sunrise + timedelta(hours=1)) is None

    def test_window_for_rejects_an_unknown_prayer(self) -> None:
        daily = windows_for()
        # Constructed with only one window, so the lookup must fail loudly
        # rather than return a wrong window.
        partial = type(daily)(
            prayer_date=daily.prayer_date,
            windows=(daily.window_for(PrayerName.FAJR),),
            sunrise=daily.sunrise,
            next_day_fajr=daily.next_day_fajr,
        )
        with pytest.raises(KeyError):
            partial.window_for(PrayerName.ISHA)


class TestOutOfOrderInput:
    def inverted(self) -> PrayerSchedule:
        """A schedule whose Isha lands after the following Fajr.

        The high-latitude fallback rules genuinely produce this in midsummer.
        """
        base = schedule_for(MAKKAH, date(2026, 7, 20))
        return PrayerSchedule(
            prayer_date=base.prayer_date,
            fajr=base.fajr,
            sunrise=base.sunrise,
            dhuhr=base.dhuhr,
            asr=base.asr,
            maghrib=base.maghrib,
            isha=base.isha + timedelta(days=2),
        )

    def test_no_window_runs_backwards(self) -> None:
        next_fajr = schedule_for(MAKKAH, date(2026, 7, 21)).fajr
        for window in compute_windows(self.inverted(), next_fajr).windows:
            assert window.ends_at >= window.starts_at, f"{window.prayer} runs backwards"

    def test_clamping_is_reported(self) -> None:
        next_fajr = schedule_for(MAKKAH, date(2026, 7, 21)).fajr
        assert compute_windows(self.inverted(), next_fajr).has_clamped_windows

    def test_well_ordered_input_is_not_flagged(self) -> None:
        assert not windows_for().has_clamped_windows

    def test_a_collapsed_window_is_empty_not_negative(self) -> None:
        next_fajr = schedule_for(MAKKAH, date(2026, 7, 21)).fajr
        isha = compute_windows(self.inverted(), next_fajr).window_for(PrayerName.ISHA)
        assert isha.duration == timedelta()
        assert isha.is_empty
        # An empty window can never be the active one, so it cannot hold a lock.
        assert not isha.contains(isha.starts_at)


class TestExtremeLatitudes:
    @pytest.mark.parametrize("prayer_date", [date(2026, 6, 21), date(2026, 12, 21)])
    def test_arctic_windows_are_ordered(self, prayer_date: date) -> None:
        for window in windows_for(TROMSO, prayer_date).windows:
            assert window.duration >= timedelta()


class TestFormatting:
    @pytest.mark.parametrize(
        ("duration", "expected"),
        [
            (timedelta(hours=3, minutes=37), "3 hours 37 minutes"),
            (timedelta(hours=1, minutes=23), "1 hour 23 minutes"),
            (timedelta(hours=2), "2 hours"),
            (timedelta(minutes=45), "45 minutes"),
            (timedelta(minutes=1), "1 minute"),
            (timedelta(), "0 minutes"),
            (timedelta(minutes=-5), "0 minutes"),
        ],
    )
    def test_format_duration(self, duration: timedelta, expected: str) -> None:
        assert format_duration(duration) == expected


class TestClientParity:
    """Properties the Dart suite asserts identically.

    Kept together so a divergence between the two implementations is easy to
    spot when one side is edited.
    """

    def test_every_prayer_has_a_boundary(self) -> None:
        boundaries = {window.prayer: window.boundary for window in windows_for().windows}
        assert boundaries == {
            PrayerName.FAJR: WindowBoundary.SUNRISE,
            PrayerName.DHUHR: WindowBoundary.ASR,
            PrayerName.ASR: WindowBoundary.MAGHRIB,
            PrayerName.MAGHRIB: WindowBoundary.ISHA,
            PrayerName.ISHA: WindowBoundary.NEXT_DAY_FAJR,
        }

    def test_windows_are_returned_in_prayer_order(self) -> None:
        assert [window.prayer for window in windows_for().windows] == [
            PrayerName.FAJR,
            PrayerName.DHUHR,
            PrayerName.ASR,
            PrayerName.MAGHRIB,
            PrayerName.ISHA,
        ]
