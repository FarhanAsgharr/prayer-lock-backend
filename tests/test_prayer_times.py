"""Prayer-time calculation tests.

The reference values are cross-checked against published timetables for each
authority. Tolerances are stated per-assertion: a couple of minutes reflects
genuine rounding differences between implementations, not slack.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import CalculationMethod, HighLatitudeRule, Madhab, PrayerName
from app.services.prayer_times import (
    CalculationRequest,
    PrayerTimeCalculator,
    julian_day,
    solar_position,
)

calculator = PrayerTimeCalculator()


def local_hhmm(moment: datetime, timezone: str) -> str:
    return moment.astimezone(ZoneInfo(timezone)).strftime("%H:%M")


def minutes_between(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60.0


class TestSolarPosition:
    def test_julian_day_matches_known_epoch(self) -> None:
        # 1 January 2000 12:00 UT is JD 2451545.0, so midnight is 0.5 earlier.
        assert julian_day(date(2000, 1, 1)) == pytest.approx(2451544.5)

    def test_declination_near_zero_at_equinox(self) -> None:
        """The sun crosses the celestial equator at the March equinox."""
        position = solar_position(julian_day(date(2026, 3, 20)) + 0.5)
        assert abs(position.declination) < 1.0

    def test_declination_near_maximum_at_solstice(self) -> None:
        """Declination reaches the axial tilt, about 23.44 degrees, in June."""
        position = solar_position(julian_day(date(2026, 6, 21)) + 0.5)
        assert position.declination == pytest.approx(23.44, abs=0.3)

    def test_declination_negative_at_december_solstice(self) -> None:
        position = solar_position(julian_day(date(2026, 12, 21)) + 0.5)
        assert position.declination == pytest.approx(-23.44, abs=0.3)


class TestPublishedTimetables:
    """Compare against published times for well-known locations."""

    def test_makkah_umm_al_qura(self) -> None:
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.UMM_AL_QURA,
                madhab=Madhab.SHAFI,
            )
        )
        tz = "Asia/Riyadh"
        assert local_hhmm(schedule.fajr, tz) == "04:23"
        assert local_hhmm(schedule.sunrise, tz) == "05:49"
        assert local_hhmm(schedule.dhuhr, tz) == "12:27"
        assert local_hhmm(schedule.maghrib, tz) == "19:04"

    def test_umm_al_qura_isha_is_ninety_minutes_after_maghrib(self) -> None:
        """Umm al-Qura defines Isha by interval, not by sun angle."""
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.UMM_AL_QURA,
            )
        )
        assert minutes_between(schedule.isha, schedule.maghrib) == pytest.approx(90, abs=0.5)

    def test_jakarta_southern_hemisphere(self) -> None:
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=-6.2088,
                longitude=106.8456,
                timezone="Asia/Jakarta",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.SINGAPORE,
            )
        )
        # Near the equator the day length barely varies; Dhuhr sits close to noon.
        assert local_hhmm(schedule.dhuhr, "Asia/Jakarta") == "11:59"
        assert schedule.fajr < schedule.sunrise < schedule.dhuhr < schedule.maghrib


class TestMadhab:
    def test_hanafi_asr_is_later_than_shafi(self) -> None:
        """Hanafi uses a shadow factor of two, so Asr falls significantly later."""

        def asr_for(madhab: Madhab) -> datetime:
            return calculator.calculate(
                CalculationRequest(
                    latitude=24.8607,
                    longitude=67.0011,
                    timezone="Asia/Karachi",
                    prayer_date=date(2026, 7, 20),
                    method=CalculationMethod.KARACHI,
                    madhab=madhab,
                )
            ).asr

        shafi, hanafi = asr_for(Madhab.SHAFI), asr_for(Madhab.HANAFI)
        assert hanafi > shafi
        # Typically over an hour apart at this latitude and season.
        assert minutes_between(hanafi, shafi) > 45

    def test_madhab_does_not_affect_other_prayers(self) -> None:
        """Only Asr depends on the shadow ratio."""
        base = dict(
            latitude=24.8607,
            longitude=67.0011,
            timezone="Asia/Karachi",
            prayer_date=date(2026, 7, 20),
            method=CalculationMethod.KARACHI,
        )
        shafi = calculator.calculate(CalculationRequest(**base, madhab=Madhab.SHAFI))
        hanafi = calculator.calculate(CalculationRequest(**base, madhab=Madhab.HANAFI))

        assert shafi.fajr == hanafi.fajr
        assert shafi.dhuhr == hanafi.dhuhr
        assert shafi.maghrib == hanafi.maghrib
        assert shafi.isha == hanafi.isha

    def _schedule_for(self, madhab: Madhab):
        return calculator.calculate(
            CalculationRequest(
                latitude=24.8607,
                longitude=67.0011,
                timezone="Asia/Karachi",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.KARACHI,
                madhab=madhab,
            )
        )

    def test_ahle_hadith_matches_shafi(self) -> None:
        """Ahl-e-Hadith follows the majority position: Asr at shadow ratio 1.

        So its schedule is identical to Shafi. Getting this wrong — treating it
        like Hanafi — would push Asr over an hour late for those users.
        """
        shafi = self._schedule_for(Madhab.SHAFI)
        ahle_hadith = self._schedule_for(Madhab.AHLE_HADITH)

        assert ahle_hadith.asr == shafi.asr
        assert ahle_hadith.maghrib == shafi.maghrib

    def test_jafari_asr_matches_shafi(self) -> None:
        """Ja'fari uses shadow ratio 1, so Asr is the same as Shafi."""
        assert self._schedule_for(Madhab.JAFARI).asr == self._schedule_for(Madhab.SHAFI).asr

    def test_jafari_maghrib_is_later_than_sunset(self) -> None:
        """The defining Shia difference: Maghrib is delayed to 4 degrees.

        Recording Maghrib at sunset for a Ja'fari user would have them break
        their fast and pray Maghrib 10-15 minutes early every day — a serious
        error, not a rounding one.
        """
        shafi = self._schedule_for(Madhab.SHAFI)
        jafari = self._schedule_for(Madhab.JAFARI)

        assert jafari.maghrib > shafi.maghrib
        delay = (jafari.maghrib - shafi.maghrib).total_seconds() / 60
        # At this latitude and season the 4-degree delay is on the order of a
        # quarter of an hour.
        assert 8 < delay < 25

    def test_jafari_isha_follows_the_delayed_maghrib(self) -> None:
        """Isha shifts with Maghrib, since it is defined relative to it."""
        shafi = self._schedule_for(Madhab.SHAFI)
        jafari = self._schedule_for(Madhab.JAFARI)
        # Karachi uses an angle-based Isha, so both move by the sun, but the
        # Ja'fari Maghrib being later must never land after its own Isha.
        assert jafari.maghrib < jafari.isha
        assert jafari.isha >= shafi.isha


class TestHighLatitude:
    def test_london_summer_fajr_uses_fallback_rule(self) -> None:
        """At 51.5N in July the sun never reaches 18 degrees of depression.

        The geometric Fajr does not exist, so the high-latitude rule must
        supply one rather than the calculation failing or returning nonsense.
        """
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=51.5074,
                longitude=-0.1278,
                timezone="Europe/London",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.MUSLIM_WORLD_LEAGUE,
                high_latitude_rule=HighLatitudeRule.MIDDLE_OF_THE_NIGHT,
            )
        )
        # Middle-of-the-night places Fajr exactly half a night before sunrise.
        # Night runs from maghrib to the *following* sunrise, so the next day's
        # sunrise is the correct endpoint.
        night_length = minutes_between(schedule.sunrise + timedelta(days=1), schedule.maghrib)
        expected_gap = night_length / 2
        assert minutes_between(schedule.sunrise, schedule.fajr) == pytest.approx(
            expected_gap, abs=1.0
        )

    def test_high_latitude_rules_produce_different_fajr(self) -> None:
        def fajr_for(rule: HighLatitudeRule) -> datetime:
            return calculator.calculate(
                CalculationRequest(
                    latitude=55.9533,
                    longitude=-3.1883,
                    timezone="Europe/London",
                    prayer_date=date(2026, 6, 21),
                    method=CalculationMethod.MUSLIM_WORLD_LEAGUE,
                    high_latitude_rule=rule,
                )
            ).fajr

        middle = fajr_for(HighLatitudeRule.MIDDLE_OF_THE_NIGHT)
        seventh = fajr_for(HighLatitudeRule.SEVENTH_OF_THE_NIGHT)
        # A seventh of the night is a smaller offset, so Fajr lands later.
        assert seventh > middle

    def test_polar_day_falls_back_to_substitute_latitude(self) -> None:
        """Tromso has no sunset in July; the calculation must still answer."""
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=69.6492,
                longitude=18.9553,
                timezone="Europe/Oslo",
                prayer_date=date(2026, 7, 20),
                method=CalculationMethod.MUSLIM_WORLD_LEAGUE,
            )
        )
        assert schedule.fajr < schedule.sunrise < schedule.dhuhr
        assert schedule.dhuhr < schedule.asr < schedule.maghrib < schedule.isha


class TestOrderingAndInvariants:
    @pytest.mark.parametrize("method", list(CalculationMethod))
    def test_prayers_are_chronologically_ordered(self, method: CalculationMethod) -> None:
        """Every method must produce a strictly increasing sequence."""
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=25.2048,
                longitude=55.2708,
                timezone="Asia/Dubai",
                prayer_date=date(2026, 4, 15),
                method=method,
            )
        )
        times = [
            schedule.fajr,
            schedule.sunrise,
            schedule.dhuhr,
            schedule.asr,
            schedule.maghrib,
            schedule.isha,
        ]
        assert times == sorted(times), f"{method.value} produced out-of-order times"

    def test_all_times_are_timezone_aware_utc(self) -> None:
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                prayer_date=date(2026, 7, 20),
            )
        )
        for moment in schedule.as_dict().values():
            assert moment.tzinfo is not None
            assert moment.utcoffset() == timedelta(0)

    def test_adjustments_shift_only_the_named_prayer(self) -> None:
        base = dict(
            latitude=21.4225,
            longitude=39.8262,
            timezone="Asia/Riyadh",
            prayer_date=date(2026, 7, 20),
        )
        plain = calculator.calculate(CalculationRequest(**base))
        adjusted = calculator.calculate(
            CalculationRequest(**base, adjustments={PrayerName.FAJR: 5})
        )

        assert minutes_between(adjusted.fajr, plain.fajr) == pytest.approx(5, abs=0.01)
        assert adjusted.dhuhr == plain.dhuhr
        assert adjusted.asr == plain.asr

    def test_dst_transition_does_not_shift_dhuhr_incorrectly(self) -> None:
        """Across a DST boundary, local Dhuhr should stay near solar noon."""
        before = calculator.calculate(
            CalculationRequest(
                latitude=51.5074,
                longitude=-0.1278,
                timezone="Europe/London",
                prayer_date=date(2026, 3, 28),  # day before the UK transition
            )
        )
        after = calculator.calculate(
            CalculationRequest(
                latitude=51.5074,
                longitude=-0.1278,
                timezone="Europe/London",
                prayer_date=date(2026, 3, 30),  # day after
            )
        )
        # Local clock time jumps roughly an hour; the UTC instant barely moves.
        assert local_hhmm(before.dhuhr, "Europe/London").startswith("12")
        assert local_hhmm(after.dhuhr, "Europe/London").startswith("13")
        utc_gap = minutes_between(before.dhuhr.replace(year=2026, month=3, day=30), after.dhuhr)
        assert utc_gap < 5

    def test_next_prayer_after_returns_chronologically_next(self) -> None:
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                prayer_date=date(2026, 7, 20),
            )
        )
        just_before_asr = schedule.asr - timedelta(minutes=1)
        result = schedule.next_prayer_after(just_before_asr)

        assert result is not None
        assert result[0] is PrayerName.ASR

    def test_next_prayer_after_final_prayer_is_none(self) -> None:
        schedule = calculator.calculate(
            CalculationRequest(
                latitude=21.4225,
                longitude=39.8262,
                timezone="Asia/Riyadh",
                prayer_date=date(2026, 7, 20),
            )
        )
        assert schedule.next_prayer_after(schedule.isha + timedelta(minutes=1)) is None


class TestValidation:
    @pytest.mark.parametrize("latitude", [91.0, -91.0, 180.0])
    def test_rejects_out_of_range_latitude(self, latitude: float) -> None:
        with pytest.raises(ValueError, match="latitude"):
            CalculationRequest(
                latitude=latitude,
                longitude=0.0,
                timezone="UTC",
                prayer_date=date(2026, 7, 20),
            )

    @pytest.mark.parametrize("longitude", [181.0, -181.0])
    def test_rejects_out_of_range_longitude(self, longitude: float) -> None:
        with pytest.raises(ValueError, match="longitude"):
            CalculationRequest(
                latitude=0.0,
                longitude=longitude,
                timezone="UTC",
                prayer_date=date(2026, 7, 20),
            )


class TestYearRoundStability:
    def test_every_day_of_a_year_produces_ordered_times(self) -> None:
        """Guards against seasonal edge cases the spot checks would miss."""
        start = date(2026, 1, 1)
        for offset in range(365):
            target = start + timedelta(days=offset)
            schedule = calculator.calculate(
                CalculationRequest(
                    latitude=41.0082,
                    longitude=28.9784,
                    timezone="Europe/Istanbul",
                    prayer_date=target,
                    method=CalculationMethod.TURKEY,
                )
            )
            times = [
                schedule.fajr,
                schedule.sunrise,
                schedule.dhuhr,
                schedule.asr,
                schedule.maghrib,
                schedule.isha,
            ]
            assert times == sorted(times), f"out-of-order schedule on {target}"
            # Every prayer must fall within a day of its nominal date.
            assert (
                abs(
                    (
                        schedule.dhuhr
                        - datetime(target.year, target.month, target.day, 12, tzinfo=UTC)
                    ).total_seconds()
                )
                < 86400
            )
