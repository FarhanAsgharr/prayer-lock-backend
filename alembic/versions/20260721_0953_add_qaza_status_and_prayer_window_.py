"""add qaza status and prayer window columns

Revision ID: ecd79cc13f67
Revises: 4e9e5cd9e6e1
Create Date: 2026-07-21 09:53:43.499915+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'ecd79cc13f67'
down_revision: str | None = '4e9e5cd9e6e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add the qaza status value. ADD VALUE cannot run inside a transaction, so
    # it uses its own autocommit connection; IF NOT EXISTS makes it re-runnable.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE prayer_status ADD VALUE IF NOT EXISTS 'qaza_completed'")

    # New window/verification columns. Nullable so existing rows stay valid.
    op.add_column(
        "prayer_history",
        sa.Column("verification_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "prayer_history",
        sa.Column("qaza_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "prayer_history",
        sa.Column("qaza_completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prayer_history", "qaza_completed_at")
    op.drop_column("prayer_history", "qaza_deadline")
    op.drop_column("prayer_history", "verification_deadline")
    # The enum value is left in place: PostgreSQL cannot drop an enum value
    # without rebuilding the type, and an unused value is harmless.
