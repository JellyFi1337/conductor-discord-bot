"""Add guild_configs.structure_channel_id for the skeleton notify channel.

Revision ID: 0068
Revises: 0067
Create Date: 2026-09-17 01:50:00.000000

Номер 0068 зафиксирован отчётом шага 1. Секция 14 уже легла на 0067 как 0069,
поэтому 0069.down_revision сдвинут на эту ревизию: цепочка 0067 → 0068 → 0069.

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0068"
down_revision: str | Sequence[str] | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "guild_configs" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("guild_configs")}
    if "structure_channel_id" not in columns:
        op.add_column("guild_configs", sa.Column("structure_channel_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "guild_configs" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("guild_configs")}
    if "structure_channel_id" in columns:
        op.drop_column("guild_configs", "structure_channel_id")
