"""Bug report channel rows for open-test /bug.

Revision ID: 0073
Revises: 0072
Create Date: 2026-09-18 21:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0073"
down_revision: str | Sequence[str] | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "bug_reports" in tables:
        return
    op.create_table(
        "bug_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=True),
        sa.Column("did", sa.String(length=400), nullable=False),
        sa.Column("happened", sa.Text(), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_bug_reports"),
    )
    op.create_index("ix_bug_reports_guild_id", "bug_reports", ["guild_id"])
    op.create_index(
        "ix_bug_reports_guild_user_created",
        "bug_reports",
        ["guild_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "bug_reports" not in tables:
        return
    op.drop_index("ix_bug_reports_guild_user_created", table_name="bug_reports")
    op.drop_index("ix_bug_reports_guild_id", table_name="bug_reports")
    op.drop_table("bug_reports")
