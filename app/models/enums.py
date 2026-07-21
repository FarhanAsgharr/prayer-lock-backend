"""Domain enumerations shared across models, schemas and services.

These are persisted as native PostgreSQL enums. Adding a value requires a
migration, which is deliberate: a silently-widened enum is a data-integrity
hazard when the mobile client and server versions drift.
"""

import enum


class PrayerName(enum.StrEnum):
    FAJR = "fajr"
    DHUHR = "dhuhr"
    ASR = "asr"
    MAGHRIB = "maghrib"
    ISHA = "isha"


class PrayerStatus(enum.StrEnum):
    """Lifecycle of a single prayer on a single day."""

    PENDING = "pending"  # window has not opened yet
    ACTIVE = "active"  # window is open, prayer not yet completed
    COMPLETED = "completed"  # completed within the prayer window
    LATE = "late"  # completed, but after the window closed
    MISSED = "missed"  # window closed with no completion
    EXCUSED = "excused"  # user marked exempt (travel, illness, menstruation)


class Madhab(enum.StrEnum):
    """School of jurisprudence, as it affects prayer-time calculation.

    Named "madhab" for continuity, though strictly it controls two things: the
    Asr shadow ratio, and — for the Ja'fari (Shia) school — the Maghrib
    definition, which is the setting of the sun's redness (~4 degrees of
    depression) rather than sunset itself.

    Ordering note: SHAFI and HANAFI are the original values and must keep their
    string forms, because rows already persisted use them.
    """

    SHAFI = "shafi"  # Shafi'i, Maliki, Hanbali — Asr at shadow ratio 1
    HANAFI = "hanafi"  # Asr at shadow ratio 2
    AHLE_HADITH = "ahle_hadith"  # Ahl-e-Hadith — follows the majority, ratio 1
    JAFARI = "jafari"  # Ja'fari (Shia) — ratio 1, Maghrib delayed to 4 degrees


class CalculationMethod(enum.StrEnum):
    """Fajr/Isha solar-depression conventions used by major authorities."""

    MUSLIM_WORLD_LEAGUE = "muslim_world_league"
    EGYPTIAN = "egyptian"
    KARACHI = "karachi"
    UMM_AL_QURA = "umm_al_qura"
    DUBAI = "dubai"
    QATAR = "qatar"
    KUWAIT = "kuwait"
    MOONSIGHTING_COMMITTEE = "moonsighting_committee"
    SINGAPORE = "singapore"
    TURKEY = "turkey"
    TEHRAN = "tehran"
    NORTH_AMERICA = "north_america"  # ISNA


class HighLatitudeRule(enum.StrEnum):
    """How to derive Fajr/Isha where the sun never reaches the required angle.

    Relevant above roughly 48 degrees latitude, where on some dates the
    geometric calculation yields no valid time at all.
    """

    MIDDLE_OF_THE_NIGHT = "middle_of_the_night"
    SEVENTH_OF_THE_NIGHT = "seventh_of_the_night"
    TWILIGHT_ANGLE = "twilight_angle"


class VerificationStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"  # provider failure; fails open, see VerificationService


class VerificationCheck(enum.StrEnum):
    """Individual signals a vision provider can be asked to evaluate.

    New checks are added here and become immediately usable by the provider
    layer without any change to the verification pipeline.
    """

    PRAYER_MAT = "prayer_mat"
    PRAYER_CAP = "prayer_cap"
    MOSQUE = "mosque"
    PRAYER_POSITION = "prayer_position"
    TASBEEH = "tasbeeh"
    QURAN = "quran"
    PRAYER_RUG = "prayer_rug"


class PlatformType(enum.StrEnum):
    ANDROID = "android"
    IOS = "ios"


class AuditAction(enum.StrEnum):
    PRAYER_STARTED = "prayer_started"
    PRAYER_COMPLETED = "prayer_completed"
    VERIFICATION_SUBMITTED = "verification_submitted"
    VERIFICATION_APPROVED = "verification_approved"
    VERIFICATION_REJECTED = "verification_rejected"
    APPS_LOCKED = "apps_locked"
    APPS_UNLOCKED = "apps_unlocked"
    EMERGENCY_UNLOCK = "emergency_unlock"
    SETTINGS_CHANGED = "settings_changed"
    LOGIN = "login"
    LOGOUT = "logout"
    TOKEN_REFRESHED = "token_refreshed"
