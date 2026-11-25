"""Polls and unique votes.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-05 06:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "polls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="public"),
        sa.Column("allow_change", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("deanonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deanonymized_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_polls")),
    )
    with op.batch_alter_table("polls", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_polls_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_polls_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_polls_ends_at"), ["ends_at"], unique=False)
        batch_op.create_index("ix_polls_guild_ends", ["guild_id", "ends_at"], unique=False)
        batch_op.create_index("ix_polls_guild_channel", ["guild_id", "channel_id"], unique=False)
    op.create_table(
        "poll_votes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("poll_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("option_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_votes")),
        sa.UniqueConstraint("poll_id", "user_id", name="uq_poll_vote"),
    )
    with op.batch_alter_table("poll_votes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_poll_votes_poll_id"), ["poll_id"], unique=False)
        batch_op.create_index("ix_poll_votes_poll", ["poll_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("poll_votes", schema=None) as batch_op:
        batch_op.drop_index("ix_poll_votes_poll")
        batch_op.drop_index(batch_op.f("ix_poll_votes_poll_id"))
    op.drop_table("poll_votes")
    with op.batch_alter_table("polls", schema=None) as batch_op:
        batch_op.drop_index("ix_polls_guild_channel")
        batch_op.drop_index("ix_polls_guild_ends")
        batch_op.drop_index(batch_op.f("ix_polls_ends_at"))
        batch_op.drop_index(batch_op.f("ix_polls_owner_id"))
        batch_op.drop_index(batch_op.f("ix_polls_guild_id"))
    op.drop_table("polls")
