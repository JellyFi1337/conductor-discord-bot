"""Poll channel and ping role settings.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-07 02:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "poll_settings" in tables:
        return
    op.create_table(
        "poll_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("ping_role_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_poll_settings")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "poll_settings" in tables:
        op.drop_table("poll_settings")
