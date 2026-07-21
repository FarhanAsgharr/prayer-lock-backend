#!/usr/bin/env python
"""Regenerate the Dart/Python prayer-time parity fixtures.

The mobile app computes prayer times on-device so it works offline; the server
computes them to schedule push reminders. If the two implementations drift, a
user receives a notification at one time and a device lock at another. These
fixtures are what prevent that.

Run from the backend directory after changing either implementation:

    ./.venv/bin/python scripts/generate_parity_fixtures.py

Then run the Dart side to confirm agreement:

    cd ../mobile && flutter test test/unit/prayer_time_parity_test.dart

Never edit the generated JSON by hand: it is the Python implementation's
output, and hand-editing it would defeat the entire purpose of the check.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models.enums import CalculationMethod, HighLatitudeRule, Madhab
from app.services.prayer_times import (
    CalculationRequest,
    PrayerTimeCalculator,
    calculator,
)

OUTPUT_PATH = Path(__file__).resolve().parents[2] / "mobile/test/fixtures/prayer_time_parity.json"

# Chosen to span the hard cases rather than the easy ones: equatorial, both
# hemispheres, DST-observing, and two locations above the Arctic Circle where
# the geometric calculation breaks down entirely.
CITIES: list[tuple[str, float, float, str]] = [
    ("Makkah", 21.4225, 39.8262, "Asia/Riyadh"),
    ("Cairo", 30.0444, 31.2357, "Africa/Cairo"),
    ("Karachi", 24.8607, 67.0011, "Asia/Karachi"),
    ("London", 51.5074, -0.1278, "Europe/London"),
    ("NewYork", 40.7128, -74.0060, "America/New_York"),
    ("Jakarta", -6.2088, 106.8456, "Asia/Jakarta"),
    ("Istanbul", 41.0082, 28.9784, "Europe/Istanbul"),
    ("Sydney", -33.8688, 151.2093, "Australia/Sydney"),
    ("Tromso", 69.6492, 18.9553, "Europe/Oslo"),
    ("Reykjavik", 64.1466, -21.9426, "Atlantic/Reykjavik"),
]

# Solstices and equinoxes bracket the extremes of solar declination; the two
# extra dates catch mid-season and northern-hemisphere summer specifically.
DATES: list[date] = [
    date(2026, 1, 15),
    date(2026, 3, 20),
    date(2026, 6, 21),
    date(2026, 7, 20),
    date(2026, 9, 23),
    date(2026, 12, 21),
]


def build_cases() -> list[dict]:
    methods = list(CalculationMethod)
    madhabs = list(Madhab)
    rules = list(HighLatitudeRule)

    cases: list[dict] = []
    index = 0

    for city, latitude, longitude, timezone in CITIES:
        for target_date in DATES:
            # Rotate through the configuration space so that every method,
            # madhab and high-latitude rule is exercised across the matrix
            # without generating the full cartesian product.
            method = methods[index % len(methods)]
            madhab = madhabs[index % len(madhabs)]
            rule = rules[index % len(rules)]
            index += 1

            request = CalculationRequest(
                latitude=latitude,
                longitude=longitude,
                timezone=timezone,
                prayer_date=target_date,
                method=method,
                madhab=madhab,
                high_latitude_rule=rule,
            )
            schedule = calculator.calculate(request)

            # Dart has no IANA timezone database in its core library, so the
            # offset is resolved here and passed through as a number.
            utc_offset = PrayerTimeCalculator._utc_offset_hours(
                ZoneInfo(timezone), target_date
            )

            cases.append(
                {
                    "city": city,
                    "latitude": latitude,
                    "longitude": longitude,
                    "utcOffsetHours": utc_offset,
                    "date": target_date.isoformat(),
                    "method": method.value,
                    "madhab": madhab.value,
                    "highLatitudeRule": rule.value,
                    "expected": {
                        name: moment.isoformat().replace("+00:00", "Z")
                        for name, moment in {
                            "fajr": schedule.fajr,
                            "sunrise": schedule.sunrise,
                            "dhuhr": schedule.dhuhr,
                            "asr": schedule.asr,
                            "maghrib": schedule.maghrib,
                            "isha": schedule.isha,
                        }.items()
                    },
                }
            )

    return cases


def main() -> None:
    cases = build_cases()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "generatedBy": "backend/scripts/generate_parity_fixtures.py",
                "warning": "Generated file. Do not edit by hand.",
                "cases": cases,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Wrote {len(cases)} parity cases to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
