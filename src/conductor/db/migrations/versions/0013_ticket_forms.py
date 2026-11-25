"""Ticket category forms, hints, and stored answers.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05 03:55:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("hint", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("form_json", sa.Text(), nullable=False, server_default="{}"))
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("form_answers", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.drop_column("form_answers")
    with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
        batch_op.drop_column("form_json")
        batch_op.drop_column("hint")
