"""Device registration.

A device row exists per installation, not per user, so that a user with a
phone and a tablet gets one push per device rather than duplicate adhans on
one and silence on the other.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import Device
from app.schemas.tracking import DeviceRegistrationRequest


class DeviceService:
    def register(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        payload: DeviceRegistrationRequest,
    ) -> Device:
        """Create or update the device for (user_id, install_id).

        Idempotent by design: the app re-registers on every launch and whenever
        its push token or a permission state changes, so this must converge on
        the current state rather than accumulate rows.
        """
        existing = db.execute(
            select(Device).where(
                Device.user_id == user_id,
                Device.install_id == payload.install_id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.platform = payload.platform
            existing.os_version = payload.os_version
            existing.app_version = payload.app_version
            existing.fcm_token = payload.fcm_token
            existing.has_usage_stats_permission = payload.has_usage_stats_permission
            existing.has_overlay_permission = payload.has_overlay_permission
            existing.has_notification_permission = payload.has_notification_permission
            existing.has_family_controls_authorization = (
                payload.has_family_controls_authorization
            )
            db.flush()
            return existing

        device = Device(
            user_id=user_id,
            install_id=payload.install_id,
            platform=payload.platform,
            os_version=payload.os_version,
            app_version=payload.app_version,
            fcm_token=payload.fcm_token,
            has_usage_stats_permission=payload.has_usage_stats_permission,
            has_overlay_permission=payload.has_overlay_permission,
            has_notification_permission=payload.has_notification_permission,
            has_family_controls_authorization=payload.has_family_controls_authorization,
        )
        db.add(device)
        db.flush()
        return device


device_service = DeviceService()
