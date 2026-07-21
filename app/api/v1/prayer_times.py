"""Prayer schedule endpoints."""

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, general_rate_limit
from app.models.enums import PrayerName
from app.models.user import Location
from app.schemas.prayer import PrayerTimesQuery, PrayerTimesResponse
from app.services.prayer_times import CalculationRequest, calculator

router = APIRouter(prefix="/prayer-times", tags=["prayer-times"])


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
