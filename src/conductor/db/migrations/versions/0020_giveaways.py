"""Giveaways, entries, winners, and prize-role whitelist.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-05 05:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "giveaway_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("prize_role_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_giveaway_settings")),
    )
    op.create_table(
        "giveaways",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("prize", sa.String(length=200), nullable=False),
        sa.Column("prize_role_id", sa.BigInteger(), nullable=True),
        sa.Column("winners_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_shown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_giveaways")),
    )
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_giveaways_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_giveaways_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_giveaways_ends_at"), ["ends_at"], unique=False)
        batch_op.create_index("ix_giveaways_guild_ends", ["guild_id", "ends_at"], unique=False)
        batch_op.create_index("ix_giveaways_guild_channel", ["guild_id", "channel_id"], unique=False)
    op.create_table(
        "giveaway_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_giveaway_entries")),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_entry"),
    )
    with op.batch_alter_table("giveaway_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_giveaway_entries_giveaway_id"), ["giveaway_id"], unique=False)
        batch_op.create_index("ix_giveaway_entries_giveaway", ["giveaway_id"], unique=False)
    op.create_table(
        "giveaway_winners",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("giveaway_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_giveaway_winners")),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_winner"),
    )
    with op.batch_alter_table("giveaway_winners", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_giveaway_winners_giveaway_id"), ["giveaway_id"], unique=False)
        batch_op.create_index("ix_giveaway_winners_giveaway", ["giveaway_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("giveaway_winners", schema=None) as batch_op:
        batch_op.drop_index("ix_giveaway_winners_giveaway")
        batch_op.drop_index(batch_op.f("ix_giveaway_winners_giveaway_id"))
    op.drop_table("giveaway_winners")
    with op.batch_alter_table("giveaway_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_giveaway_entries_giveaway")
        batch_op.drop_index(batch_op.f("ix_giveaway_entries_giveaway_id"))
    op.drop_table("giveaway_entries")
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.drop_index("ix_giveaways_guild_channel")
        batch_op.drop_index("ix_giveaways_guild_ends")
        batch_op.drop_index(batch_op.f("ix_giveaways_ends_at"))
        batch_op.drop_index(batch_op.f("ix_giveaways_owner_id"))
        batch_op.drop_index(batch_op.f("ix_giveaways_guild_id"))
    op.drop_table("giveaways")
    op.drop_table("giveaway_settings")
