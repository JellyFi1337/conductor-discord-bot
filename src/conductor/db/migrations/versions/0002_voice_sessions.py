"""Voice session accounting table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05 00:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_sessions")),
    )
    with op.batch_alter_table("voice_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_voice_sessions_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_voice_sessions_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_voice_sessions_guild_user", ["guild_id", "user_id"], unique=False)
        batch_op.create_index("ix_voice_sessions_open", ["guild_id", "user_id", "left_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_voice_sessions_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("voice_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_voice_sessions_user_id"))
        batch_op.drop_index("ix_voice_sessions_open")
        batch_op.drop_index("ix_voice_sessions_guild_user")
        batch_op.drop_index(batch_op.f("ix_voice_sessions_guild_id"))
        batch_op.drop_index(batch_op.f("ix_voice_sessions_channel_id"))
    op.drop_table("voice_sessions")
