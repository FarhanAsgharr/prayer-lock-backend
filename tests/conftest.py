"""Shared pytest fixtures.

Tests run against a real PostgreSQL database rather than SQLite. The schema
relies on native enums, JSONB, INET and partial indexes; testing on a different
engine would validate a schema we do not ship.
"""

import os
import uuid
from collections.abc import Generator

import pytest

# Must be set before application modules are imported, since settings are
# cached at first access.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost:5432/prayerlock_test")
os.environ.setdefault("JWT_SECRET", "test-secret-value-not-used-in-production")
os.environ.setdefault("VISION_PROVIDER", "stub")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.api.deps import get_current_user  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, User, UserSettings  # noqa: E402

test_engine = create_engine(str(get_settings().database_url), pool_pre_ping=True)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Generator[None, None, None]:
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    yield
    Base.metadata.drop_all(test_engine)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """A session wrapped in a transaction that is rolled back after each test.

    Rolling back rather than truncating keeps tests isolated without paying to
    recreate the schema, and guarantees no test can leak state into another.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def user(db: Session) -> User:
    record = User(
        firebase_uid=f"firebase-{uuid.uuid4()}",
        email=f"{uuid.uuid4().hex[:12]}@example.com",
        display_name="Test User",
        timezone="Asia/Riyadh",
    )
    record.settings = UserSettings()
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def admin_user(db: Session) -> User:
    record = User(
        firebase_uid=f"firebase-admin-{uuid.uuid4()}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        is_admin=True,
        timezone="UTC",
    )
    record.settings = UserSettings()
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def client(db: Session, user: User) -> Generator[TestClient, None, None]:
    """Test client with the database session and current user overridden.

    Authentication itself is exercised directly against AuthService; overriding
    it here keeps every other endpoint test focused on its own behaviour.
    """
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def unauthenticated_client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
