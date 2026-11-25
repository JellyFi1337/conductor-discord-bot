"""Support badge, season summary stamp, and ladder support coefficient.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-05 11:41:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ladder_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("support_coefficient", sa.Float(), nullable=False, server_default="0"))
    with op.batch_alter_table("support_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("badge_role_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("badge_threshold", sa.Integer(), nullable=False, server_default="8"))
        batch_op.add_column(sa.Column("streak_highlight", sa.Integer(), nullable=False, server_default="8"))
    with op.batch_alter_table("support_seasons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("summary_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("support_seasons", schema=None) as batch_op:
        batch_op.drop_column("summary_at")
    with op.batch_alter_table("support_settings", schema=None) as batch_op:
        batch_op.drop_column("streak_highlight")
        batch_op.drop_column("badge_threshold")
        batch_op.drop_column("badge_role_id")
    with op.batch_alter_table("ladder_settings", schema=None) as batch_op:
        batch_op.drop_column("support_coefficient")
