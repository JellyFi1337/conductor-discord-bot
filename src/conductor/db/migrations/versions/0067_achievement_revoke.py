"""Keep achievement grants after a count drop.

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-07 13:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0067"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("achievement_grants")}
    if "revoked_at" not in columns:
        op.add_column("achievement_grants", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("achievement_grants")}
    if "revoked_at" in columns:
        op.drop_column("achievement_grants", "revoked_at")
