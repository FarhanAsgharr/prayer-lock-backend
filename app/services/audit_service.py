"""Append-only audit logging.

Every entry point writes through this module rather than constructing AuditLog
rows directly, so that the shape of an audit entry is defined in exactly one
place and cannot drift between call sites.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import AuditAction
from app.models.system import AuditLog

logger = get_logger(__name__)


def record_audit_event(
    db: Session,
    *,
    action: AuditAction,
    user_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append an audit entry to the current transaction.

    The row is flushed but not committed: audit entries commit atomically with
    the action they describe, so a rolled-back operation cannot leave a log
    claiming it happened.
    """
    entry = AuditLog(
        user_id=user_id,
        action=action,
        detail=detail,
        ip_address=ip_address,
        user_agent=user_agent[:256] if user_agent else None,
    )
    db.add(entry)
    db.flush()

    logger.info(
        "audit_event",
        action=action.value,
        user_id=str(user_id) if user_id else None,
        **(detail or {}),
    )
    return entry
