"""Member observation records.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-07 01:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "member_watch" in tables:
        return
    op.create_table(
        "member_watch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("set_by", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=400), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_watch")),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_member_watch_person"),
    )
    with op.batch_alter_table("member_watch", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_member_watch_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_member_watch_until"), ["until"], unique=False)


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "member_watch" not in tables:
        return
    with op.batch_alter_table("member_watch", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_member_watch_until"))
        batch_op.drop_index(batch_op.f("ix_member_watch_guild_id"))
    op.drop_table("member_watch")
