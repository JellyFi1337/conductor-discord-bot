"""Staff feed channel and staff_actions log.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-07 00:58:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("guild_configs")}
    if "staff_feed_channel_id" not in columns:
        op.add_column("guild_configs", sa.Column("staff_feed_channel_id", sa.BigInteger(), nullable=True))
    tables = inspect(op.get_bind()).get_table_names()
    if "staff_actions" in tables:
        return
    op.create_table(
        "staff_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_rank", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("details", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_actions")),
    )
    with op.batch_alter_table("staff_actions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_staff_actions_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(
            "ix_staff_actions_guild_actor_created", ["guild_id", "actor_id", "created_at"], unique=False
        )


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "staff_actions" in tables:
        with op.batch_alter_table("staff_actions", schema=None) as batch_op:
            batch_op.drop_index("ix_staff_actions_guild_actor_created")
            batch_op.drop_index(batch_op.f("ix_staff_actions_guild_id"))
        op.drop_table("staff_actions")
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("guild_configs")}
    if "staff_feed_channel_id" in columns:
        with op.batch_alter_table("guild_configs", schema=None) as batch_op:
            batch_op.drop_column("staff_feed_channel_id")
