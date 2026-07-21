"""Model package.

Every model is imported here so that `Base.metadata` is fully populated by a
single `import app.models`. Alembic autogenerate and the test fixtures both
depend on this being exhaustive — a model that is not imported here is
invisible to migrations.
"""

from app.db.base import Base
from app.models.blocking import (
    BlockedAppCatalogEntry,
    EmergencyUnlock,
    LockSession,
    UserBlockedApp,
)
from app.models.enums import (
    AuditAction,
    CalculationMethod,
    HighLatitudeRule,
    Madhab,
    PlatformType,
    PrayerName,
    PrayerStatus,
    VerificationCheck,
    VerificationStatus,
)
from app.models.prayer import PrayerHistory, PrayerTimes
from app.models.system import AuditLog, Notification, NotificationTemplate, RemoteConfig
from app.models.user import Device, Location, User, UserSettings
from app.models.verification import PrayerVerification, VerificationCheckResult

__all__ = [
    "Base",
    # user
    "User",
    "UserSettings",
    "Location",
    "Device",
    # prayer
    "PrayerTimes",
    "PrayerHistory",
    # verification
    "PrayerVerification",
    "VerificationCheckResult",
    # blocking
    "BlockedAppCatalogEntry",
    "UserBlockedApp",
    "LockSession",
    "EmergencyUnlock",
    # system
    "Notification",
    "NotificationTemplate",
    "AuditLog",
    "RemoteConfig",
    # enums
    "PrayerName",
    "PrayerStatus",
    "Madhab",
    "CalculationMethod",
    "HighLatitudeRule",
    "VerificationStatus",
    "VerificationCheck",
    "PlatformType",
    "AuditAction",
]
