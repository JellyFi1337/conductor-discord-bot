"""Replace stored special_locked with derived special_ever_bound.

Revision ID: 0069
Revises: 0068
Create Date: 2026-09-17 01:20:00.000000

Секция 14 писалась раньше секции 1. После 0068_setup_structure цепочка
0067 → 0068 → 0069, одна голова. В guild_permissions эта ревизия не пишет.

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0069"
down_revision: str | Sequence[str] | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "guild_access_states"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "special_ever_bound" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("special_ever_bound", sa.Boolean(), nullable=False, server_default="0"),
        )
        if "special_locked" in columns:
            op.execute(sa.text(f"UPDATE {_TABLE} SET special_ever_bound = special_locked"))
    if "special_locked" in columns:
        op.drop_column(_TABLE, "special_locked")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "special_locked" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("special_locked", sa.Boolean(), nullable=False, server_default="0"),
        )
        if "special_ever_bound" in columns:
            op.execute(sa.text(f"UPDATE {_TABLE} SET special_locked = special_ever_bound"))
    if "special_ever_bound" in columns:
        op.drop_column(_TABLE, "special_ever_bound")
