"""Temporary voice rooms created by joining a hub.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05 02:41:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_hubs",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_voice_hubs")),
    )
    op.create_table(
        "voice_rooms",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("hub_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_rooms")),
        sa.UniqueConstraint("guild_id", "owner_id", name="uq_voice_room_owner"),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_voice_room_channel"),
    )
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_voice_rooms_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_voice_rooms_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_voice_rooms_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index("ix_voice_rooms_guild_hub", ["guild_id", "hub_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.drop_index("ix_voice_rooms_guild_hub")
        batch_op.drop_index(batch_op.f("ix_voice_rooms_owner_id"))
        batch_op.drop_index(batch_op.f("ix_voice_rooms_channel_id"))
        batch_op.drop_index(batch_op.f("ix_voice_rooms_guild_id"))
    op.drop_table("voice_rooms")
    op.drop_table("voice_hubs")
