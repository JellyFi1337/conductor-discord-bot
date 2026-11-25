"""YouTube RSS feed subscriptions.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-07 03:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "youtube_feeds" in tables:
        return
    op.create_table(
        "youtube_feeds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("yt_channel_id", sa.String(length=24), nullable=False),
        sa.Column("yt_channel_title", sa.String(length=200), nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("ping_role_id", sa.BigInteger(), nullable=True),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("last_video_id", sa.String(length=32), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("etag", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_youtube_feeds")),
        sa.UniqueConstraint("guild_id", "yt_channel_id", name="uq_youtube_feed_channel"),
    )
    op.create_index(op.f("ix_youtube_feeds_guild_id"), "youtube_feeds", ["guild_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "youtube_feeds" not in tables:
        return
    op.drop_index(op.f("ix_youtube_feeds_guild_id"), table_name="youtube_feeds")
    op.drop_table("youtube_feeds")
