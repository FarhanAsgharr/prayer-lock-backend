"""Vision provider abstraction.

The verification pipeline depends only on `VisionProvider`. Swapping OpenAI for
Gemini, or adding a self-hosted model later, is a registry change and touches
no calling code.

Adding a new *check* (prayer cap, mosque, tasbeeh) is likewise a change to
`CHECK_DESCRIPTIONS` only: the prompt is assembled from the requested checks,
and the response schema is derived from them.
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import VerificationCheck

logger = get_logger(__name__)

# Natural-language definition of each check, injected into the prompt. Wording
# is deliberately concrete: vague criteria produce inconsistent verdicts.
CHECK_DESCRIPTIONS: dict[VerificationCheck, str] = {
    VerificationCheck.PRAYER_MAT: (
        "a Muslim prayer mat (sajjada) laid out on the floor, ready for prayer"
    ),
    VerificationCheck.PRAYER_RUG: "a prayer rug, whether laid out or folded",
    VerificationCheck.PRAYER_CAP: "a person wearing a prayer cap (topi, kufi or taqiyah)",
    VerificationCheck.MOSQUE: "the interior or exterior of a mosque",
    VerificationCheck.PRAYER_POSITION: (
        "a person in a recognised prayer posture such as standing (qiyam), "
        "bowing (ruku), or prostrating (sujud)"
    ),
    VerificationCheck.TASBEEH: "prayer beads (tasbeeh or misbaha)",
    VerificationCheck.QURAN: "a copy of the Quran, open or closed",
}


class VisionProviderError(Exception):
    """Provider was unreachable, timed out, or returned an unusable response."""


@dataclass(frozen=True)
class CheckOutcome:
    check: VerificationCheck
    detected: bool
    confidence: float  # 0.0 - 1.0
    notes: str | None = None


@dataclass(frozen=True)
class VisionResult:
    outcomes: list[CheckOutcome]
    provider: str
    model: str
    latency_ms: int
    raw_response: dict[str, Any] = field(default_factory=dict)

    def outcome_for(self, check: VerificationCheck) -> CheckOutcome | None:
        return next((o for o in self.outcomes if o.check is check), None)


class VisionProvider(abc.ABC):
    """Interface every vision backend implements."""

    name: str
    model: str

    @abc.abstractmethod
    async def analyze(
        self,
        image_base64: str,
        checks: list[VerificationCheck],
    ) -> VisionResult:
        """Evaluate `checks` against the image, returning one outcome per check."""

    # -- shared helpers ---------------------------------------------------

    @staticmethod
    def _build_prompt(checks: list[VerificationCheck]) -> str:
        """Assemble the instruction from the requested checks.

        Two properties matter here. The model is told to answer per-check
        independently, so one uncertain check cannot drag the others down. And
        it is told to report what is actually visible rather than to be
        charitable — an over-eager model defeats the purpose of verification.
        """
        lines = [f'- "{check.value}": is there {CHECK_DESCRIPTIONS[check]}?' for check in checks]
        checks_block = "\n".join(lines)

        return (
            "You are assisting a prayer-reminder application by describing what "
            "is visible in a photograph. Evaluate each item independently and "
            "report only what you can actually see. Do not give the benefit of "
            "the doubt; if an item is not clearly visible, report it as not "
            "detected.\n\n"
            f"Evaluate the following:\n{checks_block}\n\n"
            "Respond with JSON only, in exactly this shape:\n"
            '{"results": [{"check": "<name>", "detected": true|false, '
            '"confidence": <0.0-1.0>, "notes": "<brief observation>"}]}\n\n'
            "Include one entry for every item listed above. Additionally, if the "
            "image appears to be a photograph of a screen, a printed picture, or "
            "another photograph rather than a real scene, set every "
            '"detected" to false and say so in "notes".'
        )

    @staticmethod
    def _parse_outcomes(
        payload: dict[str, Any],
        requested: list[VerificationCheck],
    ) -> list[CheckOutcome]:
        """Map a provider's JSON body onto CheckOutcome values.

        A check the model omitted is treated as *not detected* rather than
        skipped. Silently dropping it would let a malformed response satisfy a
        required check by absence.
        """
        by_name: dict[str, dict[str, Any]] = {}
        for entry in payload.get("results", []):
            if isinstance(entry, dict) and isinstance(entry.get("check"), str):
                by_name[entry["check"].strip().lower()] = entry

        outcomes: list[CheckOutcome] = []
        for check in requested:
            entry = by_name.get(check.value)
            if entry is None:
                outcomes.append(
                    CheckOutcome(
                        check=check,
                        detected=False,
                        confidence=0.0,
                        notes="Provider did not return a result for this check",
                    )
                )
                continue

            raw_confidence = entry.get("confidence", 0.0)
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = 0.0

            outcomes.append(
                CheckOutcome(
                    check=check,
                    detected=bool(entry.get("detected", False)),
                    confidence=min(max(confidence, 0.0), 1.0),
                    notes=str(entry["notes"])[:500] if entry.get("notes") else None,
                )
            )
        return outcomes

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        """Pull a JSON object out of a model response.

        Models intermittently wrap JSON in prose or markdown fences despite
        instructions, so we locate the outermost object rather than assuming
        the whole response parses.
        """
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end <= start:
                raise VisionProviderError(
                    f"Response contained no JSON object: {content[:200]!r}"
                ) from None
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError as exc:
                raise VisionProviderError(f"Malformed JSON in response: {exc}") from exc


class OpenAIVisionProvider(VisionProvider):
    name = "openai"
    model = "gpt-4o"

    _ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise VisionProviderError("OpenAI API key is not configured")
        self._api_key = api_key
        if model:
            self.model = model

    async def analyze(self, image_base64: str, checks: list[VerificationCheck]) -> VisionResult:
        settings = get_settings()
        body = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "max_tokens": 700,
            # Deterministic verdicts: the same image should not pass one day
            # and fail the next.
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._build_prompt(checks)},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
        }

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.vision_timeout_seconds) as client:
                response = await client.post(
                    self._ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise VisionProviderError(f"OpenAI request failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise VisionProviderError(f"Unexpected OpenAI response shape: {exc}") from exc

        parsed = self._extract_json(content)
        return VisionResult(
            outcomes=self._parse_outcomes(parsed, checks),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            raw_response=parsed,
        )


class GeminiVisionProvider(VisionProvider):
    name = "gemini"
    model = "gemini-1.5-flash"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise VisionProviderError("Gemini API key is not configured")
        self._api_key = api_key
        if model:
            self.model = model

    @property
    def _endpoint(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        )

    async def analyze(self, image_base64: str, checks: list[VerificationCheck]) -> VisionResult:
        settings = get_settings()
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": self._build_prompt(checks)},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": image_base64,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
            },
        }

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.vision_timeout_seconds) as client:
                response = await client.post(
                    self._endpoint,
                    params={"key": self._api_key},
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise VisionProviderError(f"Gemini request failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise VisionProviderError(f"Unexpected Gemini response shape: {exc}") from exc

        parsed = self._extract_json(content)
        return VisionResult(
            outcomes=self._parse_outcomes(parsed, checks),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            raw_response=parsed,
        )


class StubVisionProvider(VisionProvider):
    """Deterministic provider for local development and tests.

    Approves by default so the full flow can be exercised without an API key.
    It is selected only when `vision_provider` is explicitly "stub", and
    `build_vision_provider` refuses to return it in production.
    """

    name = "stub"
    model = "stub-v1"

    def __init__(self, *, detected: bool = True, confidence: float = 0.95) -> None:
        self._detected = detected
        self._confidence = confidence

    async def analyze(self, image_base64: str, checks: list[VerificationCheck]) -> VisionResult:
        return VisionResult(
            outcomes=[
                CheckOutcome(
                    check=check,
                    detected=self._detected,
                    confidence=self._confidence,
                    notes="Stub provider response",
                )
                for check in checks
            ],
            provider=self.name,
            model=self.model,
            latency_ms=0,
            raw_response={"stub": True},
        )


def build_vision_provider() -> VisionProvider:
    """Construct the configured provider.

    Refusing the stub in production is a deliberate guard: a misconfigured
    deployment would otherwise approve every verification silently.
    """
    settings = get_settings()

    if settings.vision_provider == "openai":
        return OpenAIVisionProvider(api_key=settings.openai_api_key or "")
    if settings.vision_provider == "gemini":
        return GeminiVisionProvider(api_key=settings.gemini_api_key or "")

    if settings.is_production:
        raise VisionProviderError(
            "The stub vision provider cannot be used in production; "
            "set VISION_PROVIDER to 'openai' or 'gemini'."
        )
    logger.warning("vision_provider_stubbed", environment=settings.environment)
    return StubVisionProvider()
