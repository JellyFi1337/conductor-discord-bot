"""Recruit links and default recruit channel.

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-07 05:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0060"
down_revision: str | Sequence[str] | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "recruit_channel_id" not in columns:
        op.add_column("guild_configs", sa.Column("recruit_channel_id", sa.BigInteger(), nullable=True))
    if "recruit_links" in tables:
        return
    op.create_table(
        "recruit_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recruit_links")),
        sa.UniqueConstraint("code", name="uq_recruit_links_code"),
    )
    op.create_index("ix_recruit_links_guild_id", "recruit_links", ["guild_id"])
    op.create_index("ix_recruit_links_owner", "recruit_links", ["guild_id", "owner_user_id"])
    op.create_index(
        "uq_recruit_links_active_owner",
        "recruit_links",
        ["guild_id", "owner_user_id"],
        unique=True,
        sqlite_where=sa.text("revoked_at IS NULL AND owner_user_id <> 0"),
        postgresql_where=sa.text("revoked_at IS NULL AND owner_user_id <> 0"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "recruit_links" in tables:
        indexes = {item["name"] for item in inspect(bind).get_indexes("recruit_links")}
        for name in ("uq_recruit_links_active_owner", "ix_recruit_links_owner", "ix_recruit_links_guild_id"):
            if name in indexes:
                op.drop_index(name, table_name="recruit_links")
        op.drop_table("recruit_links")
    if "recruit_channel_id" in columns:
        with op.batch_alter_table("guild_configs", schema=None) as batch_op:
            batch_op.drop_column("recruit_channel_id")
