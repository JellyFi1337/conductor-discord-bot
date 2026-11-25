"""Persistent panel rows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-05 01:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "panels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panels")),
        sa.UniqueConstraint("guild_id", "message_id", name="uq_panel_message"),
    )
    with op.batch_alter_table("panels", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_panels_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_panels_message_id"), ["message_id"], unique=False)
        batch_op.create_index("ix_panels_guild_state", ["guild_id", "state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("panels", schema=None) as batch_op:
        batch_op.drop_index("ix_panels_guild_state")
        batch_op.drop_index(batch_op.f("ix_panels_message_id"))
        batch_op.drop_index(batch_op.f("ix_panels_guild_id"))
    op.drop_table("panels")
