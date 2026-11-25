"""Ticket priority, notes, autoclose, escalate, rating, reopen.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-05 04:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"))
        batch_op.add_column(sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("autoclose_warned_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("rating", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("staff_notes", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.drop_column("staff_notes")
        batch_op.drop_column("rating")
        batch_op.drop_column("close_requested_at")
        batch_op.drop_column("escalated_at")
        batch_op.drop_column("autoclose_warned_at")
        batch_op.drop_column("last_activity_at")
        batch_op.drop_column("priority")
