"""Channel rename window.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-07 08:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "rename_state" in set(inspector.get_table_names()):
        return
    op.create_table(
        "rename_state",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("pending_name", sa.String(length=100), nullable=True),
        sa.Column("renamed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", "channel_id", name="pk_rename_state"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "rename_state" not in set(inspector.get_table_names()):
        return
    op.drop_table("rename_state")
