"""Voice room access: ghost, join code expiry, kicks, requests.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05 03:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ghosted", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "voice_room_kicks",
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("room_id", "user_id", name=op.f("pk_voice_room_kicks")),
    )
    op.create_table(
        "voice_room_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_room_requests")),
        sa.UniqueConstraint("room_id", "user_id", name="uq_voice_room_request"),
    )
    with op.batch_alter_table("voice_room_kicks", schema=None) as batch_op:
        batch_op.create_index("ix_voice_room_kicks_room", ["room_id"], unique=False)
    with op.batch_alter_table("voice_room_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_voice_room_requests_room_id"), ["room_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("voice_room_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_voice_room_requests_room_id"))
    with op.batch_alter_table("voice_room_kicks", schema=None) as batch_op:
        batch_op.drop_index("ix_voice_room_kicks_room")
    op.drop_table("voice_room_requests")
    op.drop_table("voice_room_kicks")
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.drop_column("code_expires_at")
        batch_op.drop_column("ghosted")
