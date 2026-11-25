"""Gatherings and RSVP waitlist.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-05 06:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gatherings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voice_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gatherings")),
    )
    with op.batch_alter_table("gatherings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_gatherings_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_gatherings_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_gatherings_starts_at"), ["starts_at"], unique=False)
        batch_op.create_index("ix_gatherings_guild_starts", ["guild_id", "starts_at"], unique=False)
        batch_op.create_index("ix_gatherings_guild_channel", ["guild_id", "channel_id"], unique=False)
    op.create_table(
        "gathering_rsvps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gathering_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("waitlisted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gathering_rsvps")),
        sa.UniqueConstraint("gathering_id", "user_id", name="uq_gathering_rsvp"),
    )
    with op.batch_alter_table("gathering_rsvps", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_gathering_rsvps_gathering_id"), ["gathering_id"], unique=False)
        batch_op.create_index("ix_gathering_rsvps_gathering", ["gathering_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("gathering_rsvps", schema=None) as batch_op:
        batch_op.drop_index("ix_gathering_rsvps_gathering")
        batch_op.drop_index(batch_op.f("ix_gathering_rsvps_gathering_id"))
    op.drop_table("gathering_rsvps")
    with op.batch_alter_table("gatherings", schema=None) as batch_op:
        batch_op.drop_index("ix_gatherings_guild_channel")
        batch_op.drop_index("ix_gatherings_guild_starts")
        batch_op.drop_index(batch_op.f("ix_gatherings_starts_at"))
        batch_op.drop_index(batch_op.f("ix_gatherings_owner_id"))
        batch_op.drop_index(batch_op.f("ix_gatherings_guild_id"))
    op.drop_table("gatherings")
