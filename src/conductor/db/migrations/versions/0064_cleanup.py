"""Drop leftover columns from 12b and 17a.

Revision ID: 0064
Revises: 0063
Create Date: 2026-09-07 07:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0064"
down_revision: str | Sequence[str] | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ladder_settings", ("support_coefficient",)),
    ("reputation_settings", ("sinner_threshold", "sinner_role_id")),
    ("voice_rooms", ("code", "code_expires_at", "lfg_need", "lfg_expires_at")),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _DROPS:
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        pending = [name for name in columns if name in existing]
        if not pending:
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name in pending:
                batch_op.drop_column(name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "ladder_settings" in tables:
        existing = {column["name"] for column in inspector.get_columns("ladder_settings")}
        if "support_coefficient" not in existing:
            with op.batch_alter_table("ladder_settings", schema=None) as batch_op:
                batch_op.add_column(sa.Column("support_coefficient", sa.Float(), nullable=False, server_default="0"))
    if "reputation_settings" in tables:
        existing = {column["name"] for column in inspector.get_columns("reputation_settings")}
        with op.batch_alter_table("reputation_settings", schema=None) as batch_op:
            if "sinner_threshold" not in existing:
                batch_op.add_column(sa.Column("sinner_threshold", sa.Integer(), nullable=False, server_default="15"))
            if "sinner_role_id" not in existing:
                batch_op.add_column(sa.Column("sinner_role_id", sa.BigInteger(), nullable=True))
    if "voice_rooms" in tables:
        existing = {column["name"] for column in inspector.get_columns("voice_rooms")}
        with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
            if "code" not in existing:
                batch_op.add_column(sa.Column("code", sa.String(length=8), nullable=True))
            if "code_expires_at" not in existing:
                batch_op.add_column(sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True))
            if "lfg_need" not in existing:
                batch_op.add_column(sa.Column("lfg_need", sa.Integer(), nullable=True))
            if "lfg_expires_at" not in existing:
                batch_op.add_column(sa.Column("lfg_expires_at", sa.DateTime(timezone=True), nullable=True))
