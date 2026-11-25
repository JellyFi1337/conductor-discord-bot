"""Pulse docks: live numbers in voice channel names.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-05 11:23:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pulse_docks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("template", sa.String(length=200), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("last_renamed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pulse_docks")),
        sa.UniqueConstraint("channel_id", name="uq_pulse_dock_channel"),
    )
    with op.batch_alter_table("pulse_docks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_pulse_docks_guild_id"), ["guild_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("pulse_docks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pulse_docks_guild_id"))
    op.drop_table("pulse_docks")
