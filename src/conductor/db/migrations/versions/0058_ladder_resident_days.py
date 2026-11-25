"""Add ladder resident_days; keep existing guild thresholds.

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-07 05:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0058"
down_revision: str | Sequence[str] | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "ladder_settings" not in tables:
        return
    columns = {column["name"] for column in inspect(bind).get_columns("ladder_settings")}
    if "resident_days" in columns:
        return
    with op.batch_alter_table("ladder_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resident_days", sa.Integer(), nullable=False, server_default="30"))


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "ladder_settings" not in tables:
        return
    columns = {column["name"] for column in inspect(bind).get_columns("ladder_settings")}
    if "resident_days" not in columns:
        return
    with op.batch_alter_table("ladder_settings", schema=None) as batch_op:
        batch_op.drop_column("resident_days")
