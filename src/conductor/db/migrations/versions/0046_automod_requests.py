"""Automod admin channel and change requests.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-07 01:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "automod_channel_id" not in columns:
        op.add_column("guild_configs", sa.Column("automod_channel_id", sa.BigInteger(), nullable=True))
    if "automod_requests" in tables:
        return
    op.create_table(
        "automod_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("rule", sa.String(length=80), nullable=False),
        sa.Column("proposal", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.String(length=400), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automod_requests")),
    )
    with op.batch_alter_table("automod_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_automod_requests_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_automod_requests_guild_created", ["guild_id", "created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "automod_requests" in tables:
        with op.batch_alter_table("automod_requests", schema=None) as batch_op:
            batch_op.drop_index("ix_automod_requests_guild_created")
            batch_op.drop_index(batch_op.f("ix_automod_requests_guild_id"))
        op.drop_table("automod_requests")
    if "automod_channel_id" in columns:
        with op.batch_alter_table("guild_configs", schema=None) as batch_op:
            batch_op.drop_column("automod_channel_id")
