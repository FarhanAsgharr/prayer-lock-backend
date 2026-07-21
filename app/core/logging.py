"""Structured logging configuration.

JSON in deployed environments so logs are queryable; human-readable locally.
A shared processor chain guarantees every event carries a timestamp, level and
logger name, which is what makes the audit requirements in the spec auditable
rather than merely present.
"""

import logging
import sys

import structlog

from app.core.config import get_settings

# Fields that must never reach a log sink, in any environment.
_REDACTED_KEYS = frozenset(
    {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "api_key",
        "openai_api_key",
        "gemini_api_key",
        "jwt_secret",
        "fcm_token",
        "image_base64",
    }
)


def _redact_sensitive(_logger: object, _method: str, event_dict: dict) -> dict:
    """Replace sensitive values before they are rendered.

    Applied as a processor rather than at call sites, because relying on every
    future call site to remember is not a security control.
    """
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if settings.debug else logging.INFO,
    )

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_sensitive,
    ]

    if settings.environment in ("local", "test"):
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
