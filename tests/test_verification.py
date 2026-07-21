"""Verification policy and vision-provider tests."""

import base64
import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from app.models.enums import (
    PrayerName,
    PrayerStatus,
    VerificationCheck,
    VerificationStatus,
)
from app.models.prayer import PrayerHistory
from app.models.user import User
from app.models.verification import PrayerVerification
from app.providers.vision import (
    CheckOutcome,
    StubVisionProvider,
    VisionProvider,
    VisionProviderError,
    VisionResult,
)
from app.services.verification_service import VerificationPolicy, VerificationService
from app.utils.image_hash import (
    ImageDecodeError,
    difference_hash,
    hamming_distance,
    hash_base64_image,
)


def make_image_base64(colour: tuple[int, int, int], size: int = 64, seed: int = 0) -> str:
    """Build a deterministic test image with some internal structure.

    A flat colour would hash identically regardless of content, which would
    make the replay tests vacuous.
    """
    image = Image.new("RGB", (size, size), colour)
    pixels = image.load()
    assert pixels is not None
    for x in range(size):
        for y in range(size):
            if (x * 7 + y * 13 + seed * 31) % 17 < 6:
                pixels[x, y] = (
                    (colour[0] + 90) % 256,
                    (colour[1] + 40) % 256,
                    (colour[2] + 160) % 256,
                )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class FailingVisionProvider(VisionProvider):
    name = "failing"
    model = "failing-v1"

    async def analyze(self, image_base64: str, checks: list[VerificationCheck]) -> VisionResult:
        raise VisionProviderError("Simulated provider outage")


class ScriptedVisionProvider(VisionProvider):
    """Returns a predetermined outcome per check."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, outcomes: dict[VerificationCheck, tuple[bool, float]]) -> None:
        self._outcomes = outcomes

    async def analyze(self, image_base64: str, checks: list[VerificationCheck]) -> VisionResult:
        return VisionResult(
            outcomes=[
                CheckOutcome(
                    check=check,
                    detected=self._outcomes.get(check, (False, 0.0))[0],
                    confidence=self._outcomes.get(check, (False, 0.0))[1],
                )
                for check in checks
            ],
            provider=self.name,
            model=self.model,
            latency_ms=1,
            raw_response={},
        )


@pytest.fixture
def prayer(db: Session, user: User) -> PrayerHistory:
    now = datetime.now(UTC)
    record = PrayerHistory(
        user_id=user.id,
        prayer_date=now.date(),
        prayer=PrayerName.DHUHR,
        status=PrayerStatus.ACTIVE,
        scheduled_at=now - timedelta(minutes=10),
        window_ends_at=now + timedelta(hours=3),
    )
    db.add(record)
    db.flush()
    return record


class TestImageHashing:
    def test_identical_images_hash_identically(self) -> None:
        payload = make_image_base64((120, 80, 40))
        assert hash_base64_image(payload) == hash_base64_image(payload)

    def test_different_scenes_hash_differently(self) -> None:
        a = hash_base64_image(make_image_base64((10, 20, 30), seed=1))
        b = hash_base64_image(make_image_base64((200, 180, 160), seed=9))
        assert hamming_distance(a, b) > 8

    def test_recompression_barely_changes_hash(self) -> None:
        """A re-encoded photo must still be recognised as the same scene.

        The fixture uses coarse blocks and a gradient rather than a fine
        pattern, because dHash downsamples to an 8x8 grid: detail finer than
        that averages away, which is the whole point of a perceptual hash.
        """
        original = Image.new("RGB", (64, 64))
        pixels = original.load()
        assert pixels is not None
        for x in range(64):
            for y in range(64):
                # Large quadrant blocks plus a horizontal gradient — structure
                # that survives downsampling, as a real photograph's does.
                block = 90 if (x // 16 + y // 16) % 2 == 0 else 30
                pixels[x, y] = (block + x * 2, block + y, block)

        lossless = io.BytesIO()
        original.save(lossless, format="PNG")
        lossy = io.BytesIO()
        original.save(lossy, format="JPEG", quality=60)

        a = difference_hash(Image.open(lossless))
        b = difference_hash(Image.open(lossy))
        assert hamming_distance(a, b) <= 8

    def test_featureless_images_collide(self) -> None:
        """Documents a real limitation rather than pretending it away.

        A flat image carries no structure to hash, so two different blank
        frames are indistinguishable. This is why a hash match is treated as a
        signal to flag and not as proof of a replay.
        """

        def flat(colour: tuple[int, int, int]) -> str:
            buffer = io.BytesIO()
            Image.new("RGB", (64, 64), colour).save(buffer, format="PNG")
            return difference_hash(Image.open(buffer))

        assert flat((0, 0, 0)) == flat((255, 255, 255))

    def test_rejects_non_image_payload(self) -> None:
        with pytest.raises(ImageDecodeError):
            hash_base64_image(base64.b64encode(b"this is not an image").decode())

    def test_rejects_invalid_base64(self) -> None:
        with pytest.raises(ImageDecodeError):
            hash_base64_image("!!!not base64!!!")

    def test_accepts_data_uri_prefix(self) -> None:
        payload = make_image_base64((50, 60, 70))
        assert hash_base64_image(f"data:image/png;base64,{payload}") == hash_base64_image(payload)

    def test_hamming_distance_rejects_mismatched_lengths(self) -> None:
        with pytest.raises(ValueError, match="different lengths"):
            hamming_distance("abcd", "abcdef")


class TestVerificationApproval:
    async def test_detected_mat_approves(self, db: Session, user: User, prayer) -> None:
        service = VerificationService(provider=StubVisionProvider(detected=True))
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is True
        assert outcome.status is VerificationStatus.APPROVED
        assert outcome.attempt_number == 1

    async def test_undetected_mat_rejects(self, db: Session, user: User, prayer) -> None:
        service = VerificationService(provider=StubVisionProvider(detected=False))
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is False
        assert outcome.status is VerificationStatus.REJECTED
        assert outcome.rejection_reason == "Prayer mat not detected. Please try again."

    async def test_low_confidence_detection_rejects(self, db: Session, user: User, prayer) -> None:
        """A detection below threshold is not good enough to unlock."""
        service = VerificationService(
            provider=ScriptedVisionProvider({VerificationCheck.PRAYER_MAT: (True, 0.3)}),
            policy=VerificationPolicy(minimum_confidence=0.6),
        )
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is False
        assert "clearly" in (outcome.rejection_reason or "")

    async def test_check_results_are_persisted(self, db: Session, user: User, prayer) -> None:
        service = VerificationService(provider=StubVisionProvider(detected=True))
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        record = db.get(PrayerVerification, outcome.verification_id)
        assert record is not None
        assert len(record.check_results) == 1
        assert record.check_results[0].check is VerificationCheck.PRAYER_MAT
        assert record.check_results[0].was_required is True


class TestMultipleChecks:
    async def test_optional_check_failure_does_not_block(
        self, db: Session, user: User, prayer
    ) -> None:
        """Optional checks are recorded but must not gate approval."""
        service = VerificationService(
            provider=ScriptedVisionProvider(
                {
                    VerificationCheck.PRAYER_MAT: (True, 0.95),
                    VerificationCheck.QURAN: (False, 0.0),
                }
            ),
            policy=VerificationPolicy(
                required_checks=(VerificationCheck.PRAYER_MAT,),
                optional_checks=(VerificationCheck.QURAN,),
            ),
        )
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is True

        record = db.get(PrayerVerification, outcome.verification_id)
        assert record is not None
        assert len(record.check_results) == 2

    async def test_additional_required_check_gates_approval(
        self, db: Session, user: User, prayer
    ) -> None:
        """Adding a required check must be configuration-only, and must work."""
        service = VerificationService(
            provider=ScriptedVisionProvider(
                {
                    VerificationCheck.PRAYER_MAT: (True, 0.95),
                    VerificationCheck.PRAYER_CAP: (False, 0.0),
                }
            ),
            policy=VerificationPolicy(
                required_checks=(
                    VerificationCheck.PRAYER_MAT,
                    VerificationCheck.PRAYER_CAP,
                )
            ),
        )
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is False

    async def test_missing_check_in_response_is_treated_as_undetected(
        self, db: Session, user: User, prayer
    ) -> None:
        """A malformed response must not satisfy a required check by omission."""
        outcomes = VisionProvider._parse_outcomes({"results": []}, [VerificationCheck.PRAYER_MAT])
        assert outcomes[0].detected is False
        assert outcomes[0].confidence == 0.0


class TestFailureHandling:
    async def test_provider_outage_fails_open(self, db: Session, user: User, prayer) -> None:
        """A provider outage must not trap the user behind a lock."""
        service = VerificationService(provider=FailingVisionProvider())
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((100, 100, 100)),
        )
        assert outcome.approved is True
        assert outcome.status is VerificationStatus.ERROR
        assert outcome.released_without_detection is True

    async def test_attempt_limit_releases_user(self, db: Session, user: User, prayer) -> None:
        """After the configured attempts the user is released, honestly recorded."""
        service = VerificationService(
            provider=StubVisionProvider(detected=False),
            policy=VerificationPolicy(max_attempts=3),
        )
        results = [
            await service.verify(
                db,
                user_id=user.id,
                prayer_history_id=prayer.id,
                image_base64=make_image_base64((10 * i, 20 * i, 30 * i), seed=i),
            )
            for i in range(1, 4)
        ]

        assert [r.approved for r in results] == [False, False, True]
        assert results[-1].released_without_detection is True
        assert results[-1].attempt_number == 3

    async def test_undecodable_image_still_reaches_provider(
        self, db: Session, user: User, prayer
    ) -> None:
        """Hashing failure must not block verification outright."""
        service = VerificationService(provider=StubVisionProvider(detected=True))
        outcome = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=base64.b64encode(b"not really an image").decode(),
        )
        assert outcome.approved is True

        record = db.get(PrayerVerification, outcome.verification_id)
        assert record is not None
        assert record.image_phash is None


class TestReplayDetection:
    async def test_resubmitted_image_is_flagged(self, db: Session, user: User, prayer) -> None:
        service = VerificationService(provider=StubVisionProvider(detected=True))
        payload = make_image_base64((90, 110, 130), seed=3)

        first = await service.verify(
            db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload
        )
        second = await service.verify(
            db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload
        )

        assert first.is_suspected_replay is False
        assert second.is_suspected_replay is True

    async def test_replay_is_flagged_but_approved_by_default(
        self, db: Session, user: User, prayer
    ) -> None:
        """Default policy records the suspicion without punishing the user.

        A false positive here would wrongly accuse someone of faking prayer,
        so the default is to observe rather than block.
        """
        service = VerificationService(provider=StubVisionProvider(detected=True))
        payload = make_image_base64((70, 70, 70), seed=5)
        await service.verify(db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload)
        second = await service.verify(
            db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload
        )
        assert second.is_suspected_replay is True
        assert second.approved is True

    async def test_replay_can_be_configured_to_reject(
        self, db: Session, user: User, prayer
    ) -> None:
        service = VerificationService(
            provider=StubVisionProvider(detected=True),
            policy=VerificationPolicy(reject_suspected_replays=True, max_attempts=10),
        )
        payload = make_image_base64((45, 55, 65), seed=7)
        await service.verify(db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload)
        second = await service.verify(
            db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload
        )
        assert second.approved is False
        assert "submitted before" in (second.rejection_reason or "")

    async def test_different_image_is_not_flagged(self, db: Session, user: User, prayer) -> None:
        service = VerificationService(provider=StubVisionProvider(detected=True))
        await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((10, 10, 10), seed=1),
        )
        second = await service.verify(
            db,
            user_id=user.id,
            prayer_history_id=prayer.id,
            image_base64=make_image_base64((240, 200, 160), seed=11),
        )
        assert second.is_suspected_replay is False

    async def test_replay_check_is_scoped_to_one_user(
        self, db: Session, user: User, prayer, admin_user: User
    ) -> None:
        """One user's submission must never taint another's."""
        service = VerificationService(provider=StubVisionProvider(detected=True))
        payload = make_image_base64((33, 66, 99), seed=13)

        await service.verify(db, user_id=user.id, prayer_history_id=prayer.id, image_base64=payload)

        other_prayer = PrayerHistory(
            user_id=admin_user.id,
            prayer_date=datetime.now(UTC).date(),
            prayer=PrayerName.ASR,
            status=PrayerStatus.ACTIVE,
            scheduled_at=datetime.now(UTC),
            window_ends_at=datetime.now(UTC) + timedelta(hours=2),
        )
        db.add(other_prayer)
        db.flush()

        outcome = await service.verify(
            db,
            user_id=admin_user.id,
            prayer_history_id=other_prayer.id,
            image_base64=payload,
        )
        assert outcome.is_suspected_replay is False


class TestPromptConstruction:
    def test_prompt_includes_every_requested_check(self) -> None:
        prompt = VisionProvider._build_prompt(
            [VerificationCheck.PRAYER_MAT, VerificationCheck.TASBEEH]
        )
        assert "prayer_mat" in prompt
        assert "tasbeeh" in prompt
        assert "prayer beads" in prompt

    def test_prompt_warns_against_photos_of_screens(self) -> None:
        """Cheapest bypass is photographing a picture; the prompt addresses it."""
        prompt = VisionProvider._build_prompt([VerificationCheck.PRAYER_MAT])
        assert "photograph of a screen" in prompt

    def test_every_check_has_a_description(self) -> None:
        """A check without a description would silently produce an empty prompt."""
        from app.providers.vision import CHECK_DESCRIPTIONS

        for check in VerificationCheck:
            assert check in CHECK_DESCRIPTIONS, f"{check.value} has no description"


class TestResponseParsing:
    def test_parses_markdown_fenced_json(self) -> None:
        payload = VisionProvider._extract_json(
            '```json\n{"results": [{"check": "prayer_mat", "detected": true}]}\n```'
        )
        assert payload["results"][0]["detected"] is True

    def test_parses_json_embedded_in_prose(self) -> None:
        payload = VisionProvider._extract_json(
            'Here is the result: {"results": []} Hope that helps.'
        )
        assert payload == {"results": []}

    def test_raises_on_unparseable_response(self) -> None:
        with pytest.raises(VisionProviderError):
            VisionProvider._extract_json("I cannot help with that.")

    def test_confidence_is_clamped_to_valid_range(self) -> None:
        outcomes = VisionProvider._parse_outcomes(
            {"results": [{"check": "prayer_mat", "detected": True, "confidence": 5.0}]},
            [VerificationCheck.PRAYER_MAT],
        )
        assert outcomes[0].confidence == 1.0

    def test_non_numeric_confidence_becomes_zero(self) -> None:
        outcomes = VisionProvider._parse_outcomes(
            {"results": [{"check": "prayer_mat", "detected": True, "confidence": "high"}]},
            [VerificationCheck.PRAYER_MAT],
        )
        assert outcomes[0].confidence == 0.0


class TestProviderConstruction:
    def test_stub_is_refused_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A misconfigured production deploy must fail loudly, not approve all."""
        from app.core.config import Settings
        from app.core.config import get_settings as cached_settings
        from app.providers import vision

        cached_settings.cache_clear()
        monkeypatch.setattr(
            vision,
            "get_settings",
            lambda: Settings(
                environment="production",
                vision_provider="stub",
                jwt_secret="a-real-production-secret-value",
            ),
        )
        with pytest.raises(VisionProviderError, match="cannot be used in production"):
            vision.build_vision_provider()

    def test_openai_provider_requires_api_key(self) -> None:
        from app.providers.vision import OpenAIVisionProvider

        with pytest.raises(VisionProviderError, match="API key"):
            OpenAIVisionProvider(api_key="")

    def test_gemini_provider_requires_api_key(self) -> None:
        from app.providers.vision import GeminiVisionProvider

        with pytest.raises(VisionProviderError, match="API key"):
            GeminiVisionProvider(api_key="")
