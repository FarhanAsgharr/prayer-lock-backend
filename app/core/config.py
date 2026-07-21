"""Application configuration.

All settings are sourced from environment variables so that no secret is ever
committed to the repository. `.env` is read only as a developer convenience;
in production the values come from the deployment environment.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application -----------------------------------------------------
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # --- Datastores ------------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql+psycopg://localhost:5432/prayerlock",  # type: ignore[arg-type]
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")  # type: ignore[arg-type]

    # --- Auth ------------------------------------------------------------
    # Signing key for our own JWTs. Firebase verifies the *identity*; we issue
    # our own short-lived access tokens so the mobile client never has to hold
    # a long-lived credential.
    jwt_secret: str = Field(default="dev-only-insecure-secret-change-me", min_length=16)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days

    # Path to the Firebase service-account JSON. Absent in local/test, where
    # token verification is stubbed by the auth service.
    firebase_credentials_path: str | None = None

    # --- AI vision -------------------------------------------------------
    vision_provider: Literal["openai", "gemini", "stub"] = "stub"
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    vision_timeout_seconds: float = 20.0

    # --- Safety ----------------------------------------------------------
    # Packages that must never be blockable, so a locked-out user can always
    # reach emergency services. Enforced server-side as well as on-device.
    emergency_allowlist: tuple[str, ...] = (
        "com.android.dialer",
        "com.google.android.dialer",
        "com.android.server.telecom",
        "com.android.emergency",
        "com.android.settings",
    )

    @field_validator("jwt_secret")
    @classmethod
    def _reject_default_secret_in_prod(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        env = info.data.get("environment")
        if env in ("staging", "production") and value.startswith("dev-only"):
            raise ValueError("jwt_secret must be set explicitly outside local/test")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed exactly once per process."""
    return Settings()
