"""Voice room amenities: linked text, LFG, merge requests.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05 03:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("text_channel_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("lfg_need", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("lfg_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_voice_rooms_text_channel_id"), ["text_channel_id"], unique=False)
    op.create_table(
        "voice_room_lfg",
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("room_id", "user_id", name=op.f("pk_voice_room_lfg")),
    )
    op.create_table(
        "voice_room_merges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_room_id", sa.Integer(), nullable=False),
        sa.Column("to_room_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_room_merges")),
        sa.UniqueConstraint("from_room_id", name="uq_voice_room_merge_from"),
    )
    with op.batch_alter_table("voice_room_lfg", schema=None) as batch_op:
        batch_op.create_index("ix_voice_room_lfg_room", ["room_id"], unique=False)
    with op.batch_alter_table("voice_room_merges", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_voice_room_merges_to_room_id"), ["to_room_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("voice_room_merges", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_voice_room_merges_to_room_id"))
    with op.batch_alter_table("voice_room_lfg", schema=None) as batch_op:
        batch_op.drop_index("ix_voice_room_lfg_room")
    op.drop_table("voice_room_merges")
    op.drop_table("voice_room_lfg")
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_voice_rooms_text_channel_id"))
        batch_op.drop_column("lfg_expires_at")
        batch_op.drop_column("lfg_need")
        batch_op.drop_column("text_channel_id")
