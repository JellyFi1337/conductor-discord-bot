"""User-log keep days and truncated voice sessions.

Revision ID: 0065
Revises: 0064
Create Date: 2026-09-07 08:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "guild_configs" in tables:
        columns = {column["name"] for column in inspector.get_columns("guild_configs")}
        if "user_log_keep_days" not in columns:
            op.add_column(
                "guild_configs",
                sa.Column("user_log_keep_days", sa.Integer(), nullable=False, server_default="90"),
            )
    if "voice_sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("voice_sessions")}
        if "truncated" not in columns:
            op.add_column(
                "voice_sessions",
                sa.Column("truncated", sa.Boolean(), nullable=False, server_default="0"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "voice_sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("voice_sessions")}
        if "truncated" in columns:
            op.drop_column("voice_sessions", "truncated")
    if "guild_configs" in tables:
        columns = {column["name"] for column in inspector.get_columns("guild_configs")}
        if "user_log_keep_days" in columns:
            op.drop_column("guild_configs", "user_log_keep_days")
