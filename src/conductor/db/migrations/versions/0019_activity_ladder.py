"""Activity ladder roles, thresholds, and current step.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-05 05:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ladder_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guest_role_id", sa.BigInteger(), nullable=True),
        sa.Column("regular_role_id", sa.BigInteger(), nullable=True),
        sa.Column("resident_role_id", sa.BigInteger(), nullable=True),
        sa.Column("friend_role_id", sa.BigInteger(), nullable=True),
        sa.Column("regular_messages", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("regular_voice_seconds", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("regular_reactions", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("resident_messages", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("resident_voice_seconds", sa.Integer(), nullable=False, server_default="180000"),
        sa.Column("resident_strong_messages", sa.Integer(), nullable=False, server_default="5000"),
        sa.Column("resident_strong_voice_seconds", sa.Integer(), nullable=False, server_default="720000"),
        sa.Column("friend_tenure_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("friend_messages", sa.Integer(), nullable=False, server_default="3000"),
        sa.Column("friend_voice_seconds", sa.Integer(), nullable=False, server_default="360000"),
        sa.Column("friend_reactions", sa.Integer(), nullable=False, server_default="500"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_ladder_settings")),
    )
    op.create_table(
        "ladder",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False, server_default="guest"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ladder")),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_ladder_user"),
    )
    with op.batch_alter_table("ladder", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ladder_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ladder_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_ladder_guild_user", ["guild_id", "user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("ladder", schema=None) as batch_op:
        batch_op.drop_index("ix_ladder_guild_user")
        batch_op.drop_index(batch_op.f("ix_ladder_user_id"))
        batch_op.drop_index(batch_op.f("ix_ladder_guild_id"))
    op.drop_table("ladder")
    op.drop_table("ladder_settings")
