"""Learned create-rate allowances per guild bucket.

Revision ID: 0071
Revises: 0070
Create Date: 2026-09-17 20:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0071"
down_revision: str | Sequence[str] | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "rate_profiles" in tables:
        return
    op.create_table(
        "rate_profiles",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("allowance", sa.Integer(), nullable=False),
        sa.Column("frozen_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_429_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "bucket", name=op.f("pk_rate_profiles")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "rate_profiles" not in tables:
        return
    op.drop_table("rate_profiles")
