"""Store last sent structure-problem fingerprint on guild_configs.

Revision ID: 0070
Revises: 0069
Create Date: 2026-09-17 02:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0070"
down_revision: str | Sequence[str] | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "guild_configs" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("guild_configs")}
    if "structure_fingerprint" not in columns:
        op.add_column("guild_configs", sa.Column("structure_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "guild_configs" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("guild_configs")}
    if "structure_fingerprint" in columns:
        op.drop_column("guild_configs", "structure_fingerprint")
