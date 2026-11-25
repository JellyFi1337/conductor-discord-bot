"""Pending heavy sanctions and timed channel locks.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-05 04:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mod_intents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mod_intents")),
    )
    with op.batch_alter_table("mod_intents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mod_intents_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_mod_intents_guild_created", ["guild_id", "created_at"], unique=False)
    op.create_table(
        "channel_locks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("slowmode", sa.Integer(), nullable=False),
        sa.Column("previous_slowmode", sa.Integer(), nullable=False),
        sa.Column("previous_send", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unlock_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_locks")),
    )
    with op.batch_alter_table("channel_locks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_channel_locks_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_channel_locks_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index("ix_channel_locks_guild_unlock", ["guild_id", "unlock_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("channel_locks", schema=None) as batch_op:
        batch_op.drop_index("ix_channel_locks_guild_unlock")
        batch_op.drop_index(batch_op.f("ix_channel_locks_channel_id"))
        batch_op.drop_index(batch_op.f("ix_channel_locks_guild_id"))
    op.drop_table("channel_locks")
    with op.batch_alter_table("mod_intents", schema=None) as batch_op:
        batch_op.drop_index("ix_mod_intents_guild_created")
        batch_op.drop_index(batch_op.f("ix_mod_intents_guild_id"))
    op.drop_table("mod_intents")
