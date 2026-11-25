"""Quiet hours schedule, slowmode snapshot, watcher exemptions.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-05 07:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quiet_hours_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=True),
        sa.Column("end_minute", sa.Integer(), nullable=True),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("override", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("channels_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("extra_roles_json", sa.Text(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_quiet_hours_settings")),
    )
    op.create_table(
        "quiet_hours_applied",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_slowmode", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overwrites_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiet_hours_applied")),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_quiet_hours_channel"),
    )
    with op.batch_alter_table("quiet_hours_applied", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quiet_hours_applied_guild_id"), ["guild_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("quiet_hours_applied", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_quiet_hours_applied_guild_id"))
    op.drop_table("quiet_hours_applied")
    op.drop_table("quiet_hours_settings")
