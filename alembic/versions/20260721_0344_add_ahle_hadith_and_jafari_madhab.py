"""add ahle_hadith and jafari madhab

Revision ID: 4e9e5cd9e6e1
Revises: 092d748bc326
Create Date: 2026-07-21 03:44:34.428420+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '4e9e5cd9e6e1'
down_revision: str | None = '092d748bc326'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block, so each statement is
    # committed on its own connection. IF NOT EXISTS makes the migration safe to
    # re-run. New values are appended; existing 'shafi'/'hanafi' rows are
    # untouched.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE madhab ADD VALUE IF NOT EXISTS 'ahle_hadith'")
        op.execute("ALTER TYPE madhab ADD VALUE IF NOT EXISTS 'jafari'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type without rebuilding it.
    # A downgrade would only be safe if no row uses the value; rather than risk
    # a destructive rebuild, this is intentionally a no-op. The added values are
    # harmless if unused.
    pass
