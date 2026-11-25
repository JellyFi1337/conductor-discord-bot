"""Daily staff quotas on mod_settings.

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-07 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("mod_settings")}
    if "quota_json" in columns:
        return
    op.add_column("mod_settings", sa.Column("quota_json", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("mod_settings")}
    if "quota_json" not in columns:
        return
    with op.batch_alter_table("mod_settings", schema=None) as batch_op:
        batch_op.drop_column("quota_json")
