"""Private groups: hub category, membership, optional voice.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-05 06:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_hubs",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("silence_seconds", sa.Integer(), nullable=False, server_default="604800"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_group_hubs")),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("voice_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_groups_channel"),
    )
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_groups_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_groups_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_groups_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index("ix_groups_guild_owner", ["guild_id", "owner_id"], unique=False)
    op.create_table(
        "group_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_members")),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_member"),
    )
    with op.batch_alter_table("group_members", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_group_members_group_id"), ["group_id"], unique=False)
        batch_op.create_index("ix_group_members_group", ["group_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("group_members", schema=None) as batch_op:
        batch_op.drop_index("ix_group_members_group")
        batch_op.drop_index(batch_op.f("ix_group_members_group_id"))
    op.drop_table("group_members")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_index("ix_groups_guild_owner")
        batch_op.drop_index(batch_op.f("ix_groups_channel_id"))
        batch_op.drop_index(batch_op.f("ix_groups_owner_id"))
        batch_op.drop_index(batch_op.f("ix_groups_guild_id"))
    op.drop_table("groups")
    op.drop_table("group_hubs")
