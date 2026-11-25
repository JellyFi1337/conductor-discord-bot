"""Drop quiet hours, weekly challenge, and starboard.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-06 22:55:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("starboard_posts", schema=None) as batch_op:
        batch_op.drop_index("ix_starboard_posts_guild_board")
        batch_op.drop_index(batch_op.f("ix_starboard_posts_source_message_id"))
        batch_op.drop_index(batch_op.f("ix_starboard_posts_guild_id"))
    op.drop_table("starboard_posts")
    op.drop_table("starboard_settings")
    with op.batch_alter_table("quiet_hours_applied", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_quiet_hours_applied_guild_id"))
    op.drop_table("quiet_hours_applied")
    op.drop_table("quiet_hours_settings")
    op.drop_table("weekly_challenges")


def downgrade() -> None:
    op.create_table(
        "weekly_challenges",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_posted_week", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_weekly_challenges")),
    )
    op.create_table(
        "quiet_hours_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=True),
        sa.Column("end_minute", sa.Integer(), nullable=True),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("override", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("channels_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("extra_roles_json", sa.Text(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_quiet_hours_settings")),
    )
    op.create_table(
        "quiet_hours_applied",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_slowmode", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overwrites_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiet_hours_applied")),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_quiet_hours_channel"),
    )
    with op.batch_alter_table("quiet_hours_applied", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quiet_hours_applied_guild_id"), ["guild_id"], unique=False)
    op.create_table(
        "starboard_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("emoji_key", sa.String(length=64), nullable=False, server_default="unicode:⭐"),
        sa.Column("emoji_label", sa.String(length=64), nullable=False, server_default="⭐"),
        sa.Column("excluded_json", sa.Text(), nullable=False, server_default="[]"),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_starboard_settings")),
    )
    op.create_table(
        "starboard_posts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("board_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_starboard_posts")),
        sa.UniqueConstraint("guild_id", "source_message_id", name="uq_starboard_source"),
    )
    with op.batch_alter_table("starboard_posts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_starboard_posts_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_starboard_posts_source_message_id"), ["source_message_id"], unique=False)
        batch_op.create_index("ix_starboard_posts_guild_board", ["guild_id", "board_message_id"], unique=False)
