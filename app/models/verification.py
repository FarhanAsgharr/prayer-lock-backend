"""AI vision verification attempts and their per-check results."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.enums import VerificationCheck, VerificationStatus

if TYPE_CHECKING:
    # See the note in app.models.prayer: type-checking only, to avoid a cycle.
    from app.models.prayer import PrayerHistory


class PrayerVerification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single verification attempt against a captured image.

    Images themselves are never stored. Retaining photographs taken inside
    users' homes would be a serious privacy liability for no product benefit,
    so we keep only a perceptual hash and the model's structured verdict.
    The hash lets us detect the most common bypass — resubmitting one
    photograph every day — without retaining the image.
    """

    __tablename__ = "prayer_verifications"
    __table_args__ = (
        Index("ix_verifications_user_created", "user_id", "created_at"),
        Index("ix_verifications_image_hash", "user_id", "image_phash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    prayer_history_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("prayer_history.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"),
        default=VerificationStatus.PENDING,
        nullable=False,
    )

    # Which provider produced this verdict, for A/B comparison and incident
    # forensics when a provider silently changes model behaviour.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Perceptual hash of the submitted image (not cryptographic — near-identical
    # images produce near-identical hashes, which is the point).
    image_phash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True when this image closely matches an earlier accepted submission.
    is_suspected_replay: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Raw provider response, retained for debugging and prompt iteration.
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    prayer_history: Mapped["PrayerHistory"] = relationship(back_populates="verifications")
    check_results: Mapped[list["VerificationCheckResult"]] = relationship(
        back_populates="verification",
        cascade="all, delete-orphan",
    )


class VerificationCheckResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One signal evaluated within a verification attempt.

    Modelling checks as rows rather than columns is what makes the "add
    prayer cap / mosque / tasbeeh detection later" requirement a configuration
    change instead of a schema migration.
    """

    __tablename__ = "verification_check_results"
    __table_args__ = (Index("ix_check_results_verification", "verification_id", "check"),)

    verification_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("prayer_verifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    check: Mapped[VerificationCheck] = mapped_column(
        pg_enum(VerificationCheck, "verification_check"), nullable=False
    )
    detected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # Whether this check had to pass for the attempt to be approved.
    was_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    verification: Mapped[PrayerVerification] = relationship(back_populates="check_results")
