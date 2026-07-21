"""Prayer verification endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession, get_verification_service, verification_rate_limit
from app.models.enums import PrayerStatus
from app.models.prayer import PrayerHistory
from app.schemas.prayer import VerificationRequest, VerificationResponse
from app.services.verification_service import VerificationService

router = APIRouter(prefix="/verifications", tags=["verification"])


@router.post(
    "",
    response_model=VerificationResponse,
    dependencies=[Depends(verification_rate_limit)],
    summary="Submit a photo to verify a completed prayer",
)
async def submit_verification(
    payload: VerificationRequest,
    user: CurrentUser,
    db: DbSession,
    service: Annotated[VerificationService, Depends(get_verification_service)],
) -> VerificationResponse:
    """Verify a prayer via the configured vision provider.

    On approval the prayer is marked completed and its delay recorded, which is
    what releases the device-side lock on the next sync.
    """
    prayer = db.get(PrayerHistory, payload.prayer_history_id)

    # A missing record and another user's record return the same 404, so this
    # endpoint cannot be used to probe which prayer ids exist.
    if prayer is None or prayer.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prayer record not found",
        )

    if prayer.status in (PrayerStatus.COMPLETED, PrayerStatus.EXCUSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This prayer has already been recorded as complete",
        )

    outcome = await service.verify(
        db,
        user_id=user.id,
        prayer_history_id=prayer.id,
        image_base64=payload.image_base64,
    )

    if outcome.approved:
        _mark_completed(prayer)

    db.commit()

    attempts_remaining = max(0, service.policy.max_attempts - outcome.attempt_number)

    return VerificationResponse(
        verification_id=outcome.verification_id,
        status=outcome.status,
        approved=outcome.approved,
        attempt_number=outcome.attempt_number,
        attempts_remaining=attempts_remaining,
        message=_message_for(outcome.approved, outcome.rejection_reason),
        is_suspected_replay=outcome.is_suspected_replay,
    )


def _mark_completed(prayer: PrayerHistory) -> None:
    """Record completion, distinguishing on-time from late."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    prayer.completed_at = now
    prayer.delay_minutes = max(0, int((now - prayer.scheduled_at).total_seconds() // 60))
    prayer.status = PrayerStatus.COMPLETED if now <= prayer.window_ends_at else PrayerStatus.LATE


def _message_for(approved: bool, rejection_reason: str | None) -> str:
    if approved:
        return "Prayer verified. Your apps have been unlocked."
    return rejection_reason or "Prayer mat not detected. Please try again."
