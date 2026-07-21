"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1 import auth, prayer_times, tracking, verification
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import engine
from app.schemas.common import ErrorResponse

configure_logging()
logger = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Verify dependencies at startup.

    The database check logs on failure rather than raising. On a persistent
    host a failed check would ideally fail fast, but in serverless (Vercel) a
    raise here makes every cold start 500 — including the /health endpoint that
    is supposed to *report* the problem. Logging keeps the app serving so the
    health check can surface the real state and per-request errors stay
    specific to the failing operation.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("startup_complete", environment=settings.environment)
    except Exception as exc:  # noqa: BLE001 — startup must never crash the app
        logger.error("startup_database_unreachable", error=str(exc))
    yield
    engine.dispose()
    logger.info("shutdown_complete")


app = FastAPI(
    title="Prayer Lock AI",
    version="0.1.0",
    description="Backend service for prayer scheduling, verification and enforcement.",
    lifespan=lifespan,
    # Interactive docs are useful in development but expose the full API
    # surface, so they are disabled in production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    # The mobile client is not a browser and sends no Origin; this exists for
    # the admin panel, which is served from a known host.
    allow_origins=["http://localhost:3000"] if not settings.is_production else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Uniform validation errors.

    Pydantic's default body echoes the offending input, which for this API
    would mean logging base64 image payloads and tokens. We return only the
    field locations.
    """
    fields = [
        {"field": ".".join(str(part) for part in error["loc"]), "error": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            code="validation_error",
            message="The request body failed validation.",
            detail={"fields": fields},
        ).model_dump(),
    )


@app.get("/health", tags=["system"], summary="Liveness and dependency check")
def health() -> dict[str, str]:
    """Report service and database health.

    Used by the load balancer, so it checks the database rather than merely
    confirming the process is running.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "ok"
    except Exception as exc:  # surfaced, not raised: the check must always answer
        logger.error("health_check_database_failed", error=str(exc))
        database_status = "unavailable"

    return {
        "status": "ok" if database_status == "ok" else "degraded",
        "database": database_status,
        "environment": settings.environment,
        "version": app.version,
    }


for router in (
    auth.router,
    prayer_times.router,
    verification.router,
    tracking.router,
):
    app.include_router(router, prefix=settings.api_v1_prefix)
