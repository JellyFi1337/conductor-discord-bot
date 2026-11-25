"""Anonymous tickets via DM relay.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-07 04:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "tickets" not in tables:
        return
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("tickets")}
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        if "anonymous" not in columns:
            batch_op.add_column(sa.Column("anonymous", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "dm_channel_id" not in columns:
            batch_op.add_column(sa.Column("dm_channel_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "tickets" not in tables:
        return
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("tickets")}
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        if "dm_channel_id" in columns:
            batch_op.drop_column("dm_channel_id")
        if "anonymous" in columns:
            batch_op.drop_column("anonymous")
