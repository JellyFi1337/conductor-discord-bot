"""Watcher suspensions and weekly staff digest week key.

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-07 06:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0062"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "staff_digest_week" not in columns:
        op.add_column("guild_configs", sa.Column("staff_digest_week", sa.String(length=16), nullable=True))
    tables = inspect(bind).get_table_names()
    if "watcher_suspensions" in tables:
        return
    op.create_table(
        "watcher_suspensions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("set_by", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watcher_suspensions")),
    )
    op.create_index("ix_watcher_suspensions_guild_id", "watcher_suspensions", ["guild_id"])
    op.create_index("ix_watcher_suspensions_user_id", "watcher_suspensions", ["user_id"])
    op.create_index("ix_watcher_suspensions_guild_user", "watcher_suspensions", ["guild_id", "user_id"])
    op.create_index(
        "uq_watcher_suspensions_active",
        "watcher_suspensions",
        ["guild_id", "user_id"],
        unique=True,
        sqlite_where=sa.text("lifted_at IS NULL"),
        postgresql_where=sa.text("lifted_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "watcher_suspensions" in tables:
        op.drop_index("uq_watcher_suspensions_active", table_name="watcher_suspensions")
        op.drop_index("ix_watcher_suspensions_guild_user", table_name="watcher_suspensions")
        op.drop_index("ix_watcher_suspensions_user_id", table_name="watcher_suspensions")
        op.drop_index("ix_watcher_suspensions_guild_id", table_name="watcher_suspensions")
        op.drop_table("watcher_suspensions")
    columns = {column["name"] for column in inspect(bind).get_columns("guild_configs")}
    if "staff_digest_week" in columns:
        op.drop_column("guild_configs", "staff_digest_week")
