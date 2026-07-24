"""Prayer schedule endpoints."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, general_rate_limit
from app.models.enums import PrayerName
from app.models.user import Location
from app.schemas.prayer import (
    PrayerScheduleRangeResponse,
    PrayerScheduleResponse,
    PrayerTimesQuery,
    PrayerTimesResponse,
    PrayerWindowSchema,
)
from app.services.prayer_times import CalculationRequest, calculator
from app.services.prayer_windows import compute_windows, format_duration

router = APIRouter(prefix="/prayer-times", tags=["prayer-times"])

#: Upper bound on a prefetch range.
#:
#: Two weeks covers a normal gap in app usage plus a week of travel. Longer
#: ranges buy little — a client can compute times a month out on-device — while
#: making a single request arbitrarily expensive.
MAX_RANGE_DAYS = 31


def _build_request(
    query: PrayerTimesQuery,
    user: CurrentUser,
    db: DbSession,
) -> tuple[CalculationRequest, str]:
    """Resolve the calculation inputs for a query.

    Coordinates may be supplied explicitly to preview another location;
    otherwise the user's active location is used. Shared by every endpoint here
    so they cannot disagree about which location or settings apply.
    """
    settings = user.settings
    latitude, longitude, timezone = query.latitude, query.longitude, query.timezone

    if latitude is None or longitude is None:
        location = db.execute(
            select(Location)
            .where(Location.user_id == user.id, Location.is_active.is_(True))
            .order_by(Location.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if location is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No active location is set. Supply latitude and longitude, "
                    "or save a location first."
                ),
            )
        latitude, longitude = location.latitude, location.longitude
        timezone = timezone or location.timezone

    timezone = timezone or user.timezone
    target_date = query.prayer_date or datetime.now(UTC).date()

    request = CalculationRequest(
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        prayer_date=target_date,
        method=query.method or settings.calculation_method,
        madhab=query.madhab or settings.madhab,
        high_latitude_rule=query.high_latitude_rule or settings.high_latitude_rule,
        adjustments={
            PrayerName.FAJR: settings.fajr_adjustment_minutes,
            PrayerName.DHUHR: settings.dhuhr_adjustment_minutes,
            PrayerName.ASR: settings.asr_adjustment_minutes,
            PrayerName.MAGHRIB: settings.maghrib_adjustment_minutes,
            PrayerName.ISHA: settings.isha_adjustment_minutes,
        },
    )

    return request, timezone


@router.post(
    "",
    response_model=PrayerTimesResponse,
    dependencies=[Depends(general_rate_limit)],
    summary="Compute a prayer schedule",
)
def compute_prayer_times(
    query: PrayerTimesQuery,
    user: CurrentUser,
    db: DbSession,
) -> PrayerTimesResponse:
    """Compute one day's schedule.

    Coordinates may be supplied explicitly to preview another location;
    otherwise the user's active location is used. The identical calculation
    runs on-device, so this endpoint exists for server-side scheduling and for
    clients that want to confirm agreement — not as a dependency of the app.

    Returns instants only. For the durations derived from them, use
    `/prayer-times/schedule`.
    """
    request, timezone = _build_request(query, user, db)

    try:
        schedule = calculator.calculate(request)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Prayer times could not be calculated: {exc}",
        ) from exc

    now = datetime.now(UTC)
    upcoming = schedule.next_prayer_after(now)

    return PrayerTimesResponse(
        prayer_date=schedule.prayer_date,
        timezone=timezone,
        method=request.method,
        madhab=request.madhab,
        fajr=schedule.fajr,
        sunrise=schedule.sunrise,
        dhuhr=schedule.dhuhr,
        asr=schedule.asr,
        maghrib=schedule.maghrib,
        isha=schedule.isha,
        next_prayer=upcoming[0] if upcoming else None,
        next_prayer_at=upcoming[1] if upcoming else None,
        seconds_until_next_prayer=(int((upcoming[1] - now).total_seconds()) if upcoming else None),
    )


@router.get(
    "/today",
    response_model=PrayerTimesResponse,
    dependencies=[Depends(general_rate_limit)],
    summary="Today's schedule for the active location",
)
def today(user: CurrentUser, db: DbSession) -> PrayerTimesResponse:
    return compute_prayer_times(PrayerTimesQuery(prayer_date=date.today()), user=user, db=db)


def _schedule_response(
    request: CalculationRequest,
    next_day_request: CalculationRequest,
    timezone: str,
) -> PrayerScheduleResponse:
    """Compute one day's schedule and the durations derived from it."""
    try:
        schedule = calculator.calculate(request)
        # Isha runs to the *following* Fajr, so a second day must be computed.
        # Without it the last window of every day would have no defined end.
        next_day = calculator.calculate(next_day_request)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Prayer times could not be calculated: {exc}",
        ) from exc

    daily = compute_windows(schedule, next_day.fajr)

    return PrayerScheduleResponse(
        prayer_date=schedule.prayer_date,
        timezone=timezone,
        method=request.method,
        madhab=request.madhab,
        fajr=schedule.fajr,
        sunrise=schedule.sunrise,
        dhuhr=schedule.dhuhr,
        asr=schedule.asr,
        maghrib=schedule.maghrib,
        isha=schedule.isha,
        next_day_fajr=daily.next_day_fajr,
        windows=[
            PrayerWindowSchema(
                prayer=window.prayer,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                boundary=window.boundary.value,
                duration_minutes=window.duration_minutes,
                duration_label=format_duration(window.duration),
            )
            for window in daily.windows
        ],
        total_blocked_minutes=int(daily.total_duration.total_seconds() // 60),
        has_clamped_windows=daily.has_clamped_windows,
    )


@router.post(
    "/schedule",
    response_model=PrayerScheduleResponse,
    dependencies=[Depends(general_rate_limit)],
    summary="A day's prayer times with dynamic blocking durations",
)
def compute_schedule(
    query: PrayerTimesQuery,
    user: CurrentUser,
    db: DbSession,
) -> PrayerScheduleResponse:
    """One day's times plus the window each prayer occupies.

    The durations are computed, never configured: Fajr runs to sunrise, each of
    Dhuhr, Asr and Maghrib to the next prayer, and Isha to the following day's
    Fajr. They therefore change every day with the sun, and differ by hours
    between latitudes.
    """
    request, timezone = _build_request(query, user, db)
    next_day_request = replace(request, prayer_date=request.prayer_date + timedelta(days=1))

    return _schedule_response(request, next_day_request, timezone)


@router.post(
    "/schedule/range",
    response_model=PrayerScheduleRangeResponse,
    dependencies=[Depends(general_rate_limit)],
    summary="Consecutive days of schedules, for offline prefetch",
)
def compute_schedule_range(
    query: PrayerTimesQuery,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=7, ge=1, le=MAX_RANGE_DAYS),
) -> PrayerScheduleRangeResponse:
    """A run of days in one request.

    Exists so a client can fill its offline cache without issuing one round
    trip per day. The upper bound is deliberate: the calculation is cheap but
    unbounded ranges turn a single request into an arbitrary amount of work.
    """
    base_request, timezone = _build_request(query, user, db)

    def for_offset(offset: int) -> CalculationRequest:
        return replace(
            base_request,
            prayer_date=base_request.prayer_date + timedelta(days=offset),
        )

    return PrayerScheduleRangeResponse(
        days=[
            _schedule_response(for_offset(offset), for_offset(offset + 1), timezone)
            for offset in range(days)
        ]
    )
