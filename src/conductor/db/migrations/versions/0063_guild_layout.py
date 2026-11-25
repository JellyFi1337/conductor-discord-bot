"""Guild layout bindings and verification rules channel.

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-07 06:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0063"
down_revision: str | Sequence[str] | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("verification_settings")}
    if "rules_channel_id" not in columns:
        op.add_column("verification_settings", sa.Column("rules_channel_id", sa.BigInteger(), nullable=True))
    tables = inspector.get_table_names()
    if "guild_layout" in tables:
        return
    op.create_table(
        "guild_layout",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guild_layout")),
        sa.UniqueConstraint("guild_id", "key", name="uq_guild_layout_key"),
    )
    op.create_index("ix_guild_layout_guild_id", "guild_layout", ["guild_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "guild_layout" in tables:
        op.drop_index("ix_guild_layout_guild_id", table_name="guild_layout")
        op.drop_table("guild_layout")
    columns = {column["name"] for column in inspector.get_columns("verification_settings")}
    if "rules_channel_id" in columns:
        op.drop_column("verification_settings", "rules_channel_id")
