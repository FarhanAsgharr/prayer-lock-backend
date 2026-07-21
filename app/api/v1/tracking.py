"""Tracking, sync-upload and analytics endpoints.

These receive the records the mobile app creates offline and drains from its
sync queue, and serve the aggregated statistics the dashboard reads. Every
upload is idempotent on the client-supplied id, because the mobile queue
guarantees at-least-once delivery and will retry anything whose acknowledgement
it did not receive.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession, general_rate_limit
from app.models.enums import AuditAction
from app.schemas.tracking import (
    DashboardStatistics,
    DeviceRegistrationRequest,
    DeviceResponse,
    EmergencyUnlockUpload,
    LockSessionUpload,
    PrayerHistoryUpload,
    UploadAck,
    VerificationRecordUpload,
)
from app.services.audit_service import record_audit_event
from app.services.device_service import device_service
from app.services.tracking_service import tracking_service

router = APIRouter(tags=["tracking"])


@router.post(
    "/devices",
    response_model=DeviceResponse,
    dependencies=[Depends(general_rate_limit)],
    summary="Register or update this installation",
)
def register_device(
    payload: DeviceRegistrationRequest,
    user: CurrentUser,
    db: DbSession,
) -> DeviceResponse:
    """Idempotent on (user, install_id) so re-registration updates in place.

    Called on launch and whenever the push token or a permission state
    changes, so the server always holds the current picture of a device.
    """
    device = device_service.register(db, user_id=user.id, payload=payload)
    db.commit()
    return DeviceResponse.model_validate(device)


@router.post(
    "/prayer-history",
    response_model=UploadAck,
    dependencies=[Depends(general_rate_limit)],
    summary="Upload a prayer's outcome",
)
def upload_prayer_history(
    payload: PrayerHistoryUpload,
    user: CurrentUser,
    db: DbSession,
) -> UploadAck:
    record, duplicate = tracking_service.upsert_prayer_history(
        db, user_id=user.id, payload=payload
    )
    db.commit()
    return UploadAck(id=record.id, client_id=payload.client_id, duplicate=duplicate)


@router.post(
    "/verifications/record",
    response_model=UploadAck,
    dependencies=[Depends(general_rate_limit)],
    summary="Upload a verification attempt performed on-device",
)
def upload_verification(
    payload: VerificationRecordUpload,
    user: CurrentUser,
    db: DbSession,
) -> UploadAck:
    try:
        record, duplicate = tracking_service.record_verification(
            db, user_id=user.id, payload=payload
        )
    except LookupError:
        # The prayer this verification belongs to has not synced yet. 409 tells
        # the client to retry after its prayer-history items drain, rather than
        # discarding the record.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The related prayer has not been uploaded yet.",
        ) from None

    db.commit()
    return UploadAck(id=record.id, client_id=payload.client_id, duplicate=duplicate)


@router.post(
    "/lock-sessions",
    response_model=UploadAck,
    dependencies=[Depends(general_rate_limit)],
    summary="Upload a completed lock session",
)
def upload_lock_session(
    payload: LockSessionUpload,
    user: CurrentUser,
    db: DbSession,
) -> UploadAck:
    record, duplicate = tracking_service.record_lock_session(
        db, user_id=user.id, payload=payload
    )
    db.commit()
    return UploadAck(id=record.id, client_id=payload.client_id, duplicate=duplicate)


@router.post(
    "/emergency-unlocks",
    response_model=UploadAck,
    dependencies=[Depends(general_rate_limit)],
    summary="Upload an emergency unlock",
)
def upload_emergency_unlock(
    payload: EmergencyUnlockUpload,
    user: CurrentUser,
    db: DbSession,
) -> UploadAck:
    record, duplicate = tracking_service.record_emergency_unlock(
        db, user_id=user.id, payload=payload
    )
    db.commit()
    return UploadAck(id=record.id, client_id=payload.client_id, duplicate=duplicate)


@router.get(
    "/statistics",
    response_model=DashboardStatistics,
    dependencies=[Depends(general_rate_limit)],
    summary="Aggregated dashboard statistics for the current user",
)
def get_statistics(user: CurrentUser, db: DbSession) -> DashboardStatistics:
    """Server-side mirror of the on-device statistics.

    The device computes its own for offline use; this exists so the dashboard
    can reconcile against the server after syncing on a new install, and for
    the admin analytics. The two use identical rules — see TrackingService.
    """
    return tracking_service.statistics(db, user_id=user.id)


@router.post(
    "/analytics/session-open",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(general_rate_limit)],
    summary="Record that the user opened the app",
)
def record_session_open(user: CurrentUser, db: DbSession) -> None:
    """Lightweight engagement signal.

    Kept deliberately minimal — a timestamped audit event, no behavioural
    tracking. The product's value is helping people pray, not profiling them.
    """
    user.last_seen_at = datetime.now(UTC)
    record_audit_event(db, user_id=user.id, action=AuditAction.LOGIN,
                       detail={"event": "session_open"})
    db.commit()
