"""Prayer verification policy.

The vision provider reports what it sees; this module decides what that means.
Keeping the two separate is what lets the policy be tuned — thresholds, which
checks are required, how failures are handled — without touching provider code.

Two policy choices here are deliberate and worth stating plainly:

1. Provider failures unlock rather than lock. If our vision provider is down,
   the user has already prayed and is being held hostage by our outage. We log
   the failure and let them through.

2. There is a maximum attempt count, after which the user is released with the
   attempt recorded. A user whose lighting is poor, whose camera is broken, or
   who prays somewhere without a mat must never be permanently locked out of
   their own phone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import AuditAction, VerificationCheck, VerificationStatus
from app.models.verification import PrayerVerification, VerificationCheckResult
from app.providers.vision import (
    VisionProvider,
    VisionProviderError,
    VisionResult,
)
from app.services.audit_service import record_audit_event
from app.utils.image_hash import ImageDecodeError, hamming_distance, hash_base64_image

logger = get_logger(__name__)


@dataclass(frozen=True)
class VerificationPolicy:
    """Tunable acceptance criteria.

    Defaults require only a prayer mat, matching the specified behaviour, but
    the optional checks are wired through so enabling them later is a
    configuration change.
    """

    required_checks: tuple[VerificationCheck, ...] = (VerificationCheck.PRAYER_MAT,)
    optional_checks: tuple[VerificationCheck, ...] = ()
    # A required check must be detected at or above this confidence.
    minimum_confidence: float = 0.6
    # Attempts after which the user is released regardless of the verdict.
    max_attempts: int = 3
    # dHash bits below which two images are considered the same scene.
    # 8 of 64 bits tolerates lighting and framing shifts while still catching
    # a resubmitted photograph.
    replay_distance_threshold: int = 8
    # How far back to look for replayed images.
    replay_lookback_days: int = 7
    # Whether a suspected replay blocks approval, or is merely flagged.
    reject_suspected_replays: bool = False

    @property
    def all_checks(self) -> list[VerificationCheck]:
        # De-duplicated, order preserved.
        return list(dict.fromkeys(self.required_checks + self.optional_checks))


@dataclass(frozen=True)
class VerificationOutcome:
    verification_id: uuid.UUID
    status: VerificationStatus
    approved: bool
    attempt_number: int
    rejection_reason: str | None
    is_suspected_replay: bool
    # True when released by attempt exhaustion or provider failure rather than
    # a positive detection. Surfaced so the UI can word its message honestly.
    released_without_detection: bool


class VerificationService:
    def __init__(
        self,
        provider: VisionProvider,
        policy: VerificationPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._policy = policy or VerificationPolicy()

    @property
    def policy(self) -> VerificationPolicy:
        """The active policy, exposed so callers can report attempts remaining."""
        return self._policy

    async def verify(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        prayer_history_id: uuid.UUID,
        image_base64: str,
    ) -> VerificationOutcome:
        policy = self._policy
        attempt_number = self._next_attempt_number(db, prayer_history_id)

        image_phash, is_replay = self._assess_image(db, user_id, image_base64)

        verification = PrayerVerification(
            user_id=user_id,
            prayer_history_id=prayer_history_id,
            provider=self._provider.name,
            provider_model=self._provider.model,
            attempt_number=attempt_number,
            image_phash=image_phash,
            is_suspected_replay=is_replay,
            status=VerificationStatus.PENDING,
        )
        db.add(verification)
        db.flush()  # assign the primary key without committing

        try:
            result = await self._provider.analyze(image_base64, policy.all_checks)
        except VisionProviderError as exc:
            # Fail open. See the module docstring for why.
            logger.error(
                "vision_provider_failed",
                user_id=str(user_id),
                provider=self._provider.name,
                error=str(exc),
            )
            verification.status = VerificationStatus.ERROR
            verification.rejection_reason = f"Vision provider unavailable: {exc}"
            db.flush()
            record_audit_event(
                db,
                user_id=user_id,
                action=AuditAction.VERIFICATION_APPROVED,
                detail={
                    "verification_id": str(verification.id),
                    "reason": "provider_error_fail_open",
                    "provider": self._provider.name,
                },
            )
            return VerificationOutcome(
                verification_id=verification.id,
                status=VerificationStatus.ERROR,
                approved=True,
                attempt_number=attempt_number,
                rejection_reason=None,
                is_suspected_replay=is_replay,
                released_without_detection=True,
            )

        self._persist_check_results(db, verification, result)
        verification.latency_ms = result.latency_ms
        verification.raw_response = result.raw_response

        approved, reason = self._evaluate(result, is_replay)
        released_without_detection = False

        if not approved and attempt_number >= policy.max_attempts:
            # Release rather than trap the user; the record still shows the
            # attempts failed, so the history remains honest.
            approved = True
            released_without_detection = True
            reason = None
            logger.info(
                "verification_released_on_attempt_limit",
                user_id=str(user_id),
                attempts=attempt_number,
            )

        verification.status = (
            VerificationStatus.APPROVED if approved else VerificationStatus.REJECTED
        )
        verification.rejection_reason = reason
        db.flush()

        record_audit_event(
            db,
            user_id=user_id,
            action=(
                AuditAction.VERIFICATION_APPROVED if approved else AuditAction.VERIFICATION_REJECTED
            ),
            detail={
                "verification_id": str(verification.id),
                "attempt": attempt_number,
                "provider": result.provider,
                "model": result.model,
                "suspected_replay": is_replay,
                "released_without_detection": released_without_detection,
            },
        )

        return VerificationOutcome(
            verification_id=verification.id,
            status=verification.status,
            approved=approved,
            attempt_number=attempt_number,
            rejection_reason=reason,
            is_suspected_replay=is_replay,
            released_without_detection=released_without_detection,
        )

    # -- internals --------------------------------------------------------

    def _evaluate(self, result: VisionResult, is_replay: bool) -> tuple[bool, str | None]:
        """Apply the policy to a provider verdict."""
        policy = self._policy

        if is_replay and policy.reject_suspected_replays:
            return False, "This looks like a photo you have submitted before."

        for check in policy.required_checks:
            outcome = result.outcome_for(check)
            if outcome is None:
                return False, "Verification could not be completed. Please try again."
            if not outcome.detected:
                return False, "Prayer mat not detected. Please try again."
            if outcome.confidence < policy.minimum_confidence:
                return False, "Prayer mat not detected clearly. Please try again."

        return True, None

    def _assess_image(
        self,
        db: Session,
        user_id: uuid.UUID,
        image_base64: str,
    ) -> tuple[str | None, bool]:
        """Hash the image and check it against the user's recent submissions."""
        try:
            phash = hash_base64_image(image_base64)
        except ImageDecodeError as exc:
            # Not fatal: the provider may still handle a format Pillow cannot.
            logger.warning("image_hash_failed", user_id=str(user_id), error=str(exc))
            return None, False

        cutoff = datetime.now(UTC) - timedelta(days=self._policy.replay_lookback_days)
        recent = db.execute(
            select(PrayerVerification.image_phash)
            .where(
                PrayerVerification.user_id == user_id,
                PrayerVerification.created_at >= cutoff,
                PrayerVerification.image_phash.is_not(None),
                PrayerVerification.status == VerificationStatus.APPROVED,
            )
            .limit(200)
        ).scalars()

        for previous in recent:
            try:
                if hamming_distance(phash, previous) <= self._policy.replay_distance_threshold:
                    return phash, True
            except ValueError:
                # Hash produced by an older algorithm version; skip it.
                continue

        return phash, False

    def _persist_check_results(
        self,
        db: Session,
        verification: PrayerVerification,
        result: VisionResult,
    ) -> None:
        required = set(self._policy.required_checks)
        db.add_all(
            [
                VerificationCheckResult(
                    verification_id=verification.id,
                    check=outcome.check,
                    detected=outcome.detected,
                    confidence=outcome.confidence,
                    was_required=outcome.check in required,
                    notes=outcome.notes,
                )
                for outcome in result.outcomes
            ]
        )

    @staticmethod
    def _next_attempt_number(db: Session, prayer_history_id: uuid.UUID) -> int:
        existing = db.execute(
            select(PrayerVerification.id).where(
                PrayerVerification.prayer_history_id == prayer_history_id
            )
        ).all()
        return len(existing) + 1
