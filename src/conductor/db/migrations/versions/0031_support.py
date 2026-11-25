"""Support campaign windows and season presses.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-05 11:31:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("pause_seconds", sa.Integer(), nullable=False),
        sa.Column("reminder_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("min_account_days", sa.Integer(), nullable=False),
        sa.Column("min_tenure_hours", sa.Integer(), nullable=False),
        sa.Column("reject_sinner", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_support_settings")),
    )
    op.create_table(
        "support_seasons",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_seasons")),
        sa.UniqueConstraint("guild_id", "year", "month", name="uq_support_season_month"),
    )
    op.create_table(
        "support_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminder_id", sa.BigInteger(), nullable=True),
        sa.Column("closed_reason", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_windows")),
    )
    with op.batch_alter_table("support_windows", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_support_windows_guild_id"), ["guild_id"], unique=False)
    op.create_table(
        "support_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("window_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_events")),
        sa.UniqueConstraint("window_id", "user_id", name="uq_support_event_window_user"),
    )
    with op.batch_alter_table("support_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_support_events_season_id"), ["season_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_support_events_guild_user"), ["guild_id", "user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("support_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_support_events_guild_user"))
        batch_op.drop_index(batch_op.f("ix_support_events_season_id"))
    op.drop_table("support_events")
    with op.batch_alter_table("support_windows", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_support_windows_guild_id"))
    op.drop_table("support_windows")
    op.drop_table("support_seasons")
    op.drop_table("support_settings")
