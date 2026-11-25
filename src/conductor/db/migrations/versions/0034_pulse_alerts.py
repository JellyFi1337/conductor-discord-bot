"""Pulse alert on/off and thresholds.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-05 12:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pulse_alert_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("voice_floor", sa.Integer(), nullable=False),
        sa.Column("leave_floor", sa.Integer(), nullable=False),
        sa.Column("authors_pct", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_pulse_alert_settings")),
    )


def downgrade() -> None:
    op.drop_table("pulse_alert_settings")
