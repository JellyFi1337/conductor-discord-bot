"""LFG channel settings and posts.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-07 03:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0052"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "lfg_settings" not in tables:
        op.create_table(
            "lfg_settings",
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=True),
            sa.Column("ttl_minutes", sa.Integer(), nullable=False),
            sa.Column("ping_cooldown_minutes", sa.Integer(), nullable=False),
            sa.Column("per_user_cooldown_minutes", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_lfg_settings")),
        )
    if "lfg_posts" not in tables:
        op.create_table(
            "lfg_posts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("author_id", sa.BigInteger(), nullable=False),
            sa.Column("game_role_id", sa.BigInteger(), nullable=False),
            sa.Column("need", sa.Integer(), nullable=False),
            sa.Column("note", sa.String(length=100), nullable=False),
            sa.Column("room_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_reason", sa.String(length=16), nullable=True),
            sa.Column("game_pinged", sa.Boolean(), nullable=False),
            sa.Column("lfg_pinged", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_lfg_posts")),
        )
        op.create_index(op.f("ix_lfg_posts_guild_id"), "lfg_posts", ["guild_id"], unique=False)
        op.create_index(op.f("ix_lfg_posts_author_id"), "lfg_posts", ["author_id"], unique=False)
        op.create_index(op.f("ix_lfg_posts_expires_at"), "lfg_posts", ["expires_at"], unique=False)
        op.create_index("ix_lfg_posts_guild_created", "lfg_posts", ["guild_id", "created_at"], unique=False)
        op.create_index("ix_lfg_posts_guild_author", "lfg_posts", ["guild_id", "author_id", "closed_at"], unique=False)
        op.create_index("ix_lfg_posts_room", "lfg_posts", ["room_id"], unique=False)
    if "lfg_members" not in tables:
        op.create_table(
            "lfg_members",
            sa.Column("post_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("post_id", "user_id", name=op.f("pk_lfg_members")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "lfg_members" in tables:
        op.drop_table("lfg_members")
    if "lfg_posts" in tables:
        op.drop_index("ix_lfg_posts_room", table_name="lfg_posts")
        op.drop_index("ix_lfg_posts_guild_author", table_name="lfg_posts")
        op.drop_index("ix_lfg_posts_guild_created", table_name="lfg_posts")
        op.drop_index(op.f("ix_lfg_posts_expires_at"), table_name="lfg_posts")
        op.drop_index(op.f("ix_lfg_posts_author_id"), table_name="lfg_posts")
        op.drop_index(op.f("ix_lfg_posts_guild_id"), table_name="lfg_posts")
        op.drop_table("lfg_posts")
    if "lfg_settings" in tables:
        op.drop_table("lfg_settings")
