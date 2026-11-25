"""Automod hit log.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-07 01:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "automod_hits" in tables:
        return
    op.create_table(
        "automod_hits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automod_hits")),
    )
    with op.batch_alter_table("automod_hits", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_automod_hits_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(
            "ix_automod_hits_guild_user_created",
            ["guild_id", "user_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "automod_hits" not in tables:
        return
    with op.batch_alter_table("automod_hits", schema=None) as batch_op:
        batch_op.drop_index("ix_automod_hits_guild_user_created")
        batch_op.drop_index(batch_op.f("ix_automod_hits_guild_id"))
    op.drop_table("automod_hits")
