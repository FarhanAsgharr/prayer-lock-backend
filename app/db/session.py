"""Database engine and session management."""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# Serverless (Vercel) runs many short-lived instances, each of which would
# otherwise hold a pool of open connections and quickly exhaust the database's
# connection limit. NullPool opens and closes a connection per checkout, which
# is the correct trade there; pair it with a pooled/pgbouncer database URL.
# On a persistent host the real pool is kept.
_is_serverless = bool(os.environ.get("VERCEL"))

_engine_kwargs: dict = {
    # Verify a pooled connection is still alive before handing it out. Without
    # this, a connection severed by a proxy or database restart surfaces as a
    # request-time error rather than a transparent reconnect.
    "pool_pre_ping": True,
    "echo": settings.debug,
}
if _is_serverless:
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20

engine = create_engine(str(settings.database_url), **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    The session is rolled back and closed on any exception so a failed request
    can never leak an open transaction back into the pool.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
