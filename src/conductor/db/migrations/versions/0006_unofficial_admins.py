"""Unofficial administrator notices.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05 02:17:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unofficial_admin_notices",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("noticed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", name=op.f("pk_unofficial_admin_notices")),
    )


def downgrade() -> None:
    op.drop_table("unofficial_admin_notices")
