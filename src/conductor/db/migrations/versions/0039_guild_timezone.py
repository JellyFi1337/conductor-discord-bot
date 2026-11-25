"""Guild timezone for day and week boundaries.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-06 23:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("guild_configs")}
    if "timezone" in columns:
        return
    op.add_column(
        "guild_configs",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("guild_configs")}
    if "timezone" not in columns:
        return
    with op.batch_alter_table("guild_configs", schema=None) as batch_op:
        batch_op.drop_column("timezone")
