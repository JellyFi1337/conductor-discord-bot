"""Starboard channel, threshold, and one post per source message.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-05 06:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    with op.batch_alter_table("starboard_posts", schema=None) as batch_op:
        batch_op.drop_index("ix_starboard_posts_guild_board")
        batch_op.drop_index(batch_op.f("ix_starboard_posts_source_message_id"))
        batch_op.drop_index(batch_op.f("ix_starboard_posts_guild_id"))
    op.drop_table("starboard_posts")
    op.drop_table("starboard_settings")
