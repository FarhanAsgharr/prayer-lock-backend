"""add jafari calculation method and islamic section

Revision ID: 7c1a3b8f42d9
Revises: 5b2c9a1e7f40
Create Date: 2026-07-24 11:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c1a3b8f42d9"
down_revision: str | None = "ecd79cc13f67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block, so each statement is
    # committed on its own connection. IF NOT EXISTS makes the migration safe
    # to re-run. New values are appended; existing rows are untouched.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE calculationmethod ADD VALUE IF NOT EXISTS 'jafari'")

    # The user's Islamic section: an identity, distinct from the madhab column,
    # which remains the calculation parameter. Nullable because existing users
    # have not chosen one, and inferring a section from their madhab would be
    # putting an identity in someone's mouth — a Shafi'i madhab tells us
    # nothing about whether they identify as Shafi'i, Sufi or anything else.
    op.add_column(
        "user_settings",
        sa.Column("islamic_section", sa.String(length=32), nullable=True),
    )

    # Free-text name for users who chose "Other". Length-bounded so it cannot
    # be used as unbounded storage.
    op.add_column(
        "user_settings",
        sa.Column("islamic_section_label", sa.String(length=64), nullable=True),
    )

    # Which prayers are combined: "none", "dhuhr_asr", "maghrib_isha", "both".
    # Stored as text rather than an enum so adding a grouping later needs no
    # migration on a type that is only ever read as a whole.
    op.add_column(
        "user_settings",
        sa.Column(
            "prayer_grouping",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
    )

    # Whether one verification discharges a combined pair, or each prayer is
    # verified separately even when their windows are joined.
    op.add_column(
        "user_settings",
        sa.Column(
            "combined_verification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    # Records which grouping was in force when a prayer was logged, so a user
    # who switches modes does not retroactively rewrite their own history.
    op.add_column(
        "prayer_history",
        sa.Column("was_combined", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("prayer_history", "was_combined")
    op.drop_column("user_settings", "combined_verification")
    op.drop_column("user_settings", "prayer_grouping")
    op.drop_column("user_settings", "islamic_section_label")
    op.drop_column("user_settings", "islamic_section")

    # PostgreSQL cannot drop a value from an enum type without rebuilding it.
    # A rebuild is only safe when no row uses the value; rather than risk a
    # destructive rewrite, the added value is left in place. It is harmless if
    # unused.
