"""Ticket category kinds, visibility, and close outcome.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-07 03:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0053"
down_revision: str | Sequence[str] | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_RANKS = '["watcher","senior","chief","special"]'


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "ticket_categories" in tables:
        columns = {column["name"] for column in inspect(bind).get_columns("ticket_categories")}
        with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
            if "kind" not in columns:
                batch_op.add_column(sa.Column("kind", sa.String(length=16), nullable=False, server_default="general"))
            if "visible_ranks" not in columns:
                batch_op.add_column(sa.Column("visible_ranks", sa.Text(), nullable=False, server_default=_ALL_RANKS))
            if "topic" not in columns:
                batch_op.add_column(sa.Column("topic", sa.String(length=32), nullable=False, server_default=""))
            if "info_text" not in columns:
                batch_op.add_column(sa.Column("info_text", sa.Text(), nullable=False, server_default=""))
            if "hidden" not in columns:
                batch_op.add_column(sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "min_tenure_days" not in columns:
                batch_op.add_column(sa.Column("min_tenure_days", sa.Integer(), nullable=False, server_default="0"))
            if "min_ladder_step" not in columns:
                batch_op.add_column(
                    sa.Column("min_ladder_step", sa.String(length=16), nullable=False, server_default="guest")
                )
            if "excluded_user_field" not in columns:
                batch_op.add_column(
                    sa.Column("excluded_user_field", sa.String(length=80), nullable=False, server_default="")
                )
        op.execute(sa.text("UPDATE ticket_categories SET kind = 'general' WHERE kind IS NULL OR kind = ''"))
        op.execute(
            sa.text(
                f"UPDATE ticket_categories SET visible_ranks = '{_ALL_RANKS}' "
                "WHERE visible_ranks IS NULL OR visible_ranks = '' OR visible_ranks = '[]'"
            )
        )
    if "tickets" in tables:
        columns = {column["name"] for column in inspect(bind).get_columns("tickets")}
        with op.batch_alter_table("tickets", schema=None) as batch_op:
            if "outcome" not in columns:
                batch_op.add_column(sa.Column("outcome", sa.String(length=16), nullable=True))
            if "subject_user_id" not in columns:
                batch_op.add_column(sa.Column("subject_user_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "tickets" in tables:
        columns = {column["name"] for column in inspect(bind).get_columns("tickets")}
        with op.batch_alter_table("tickets", schema=None) as batch_op:
            if "subject_user_id" in columns:
                batch_op.drop_column("subject_user_id")
            if "outcome" in columns:
                batch_op.drop_column("outcome")
    if "ticket_categories" in tables:
        columns = {column["name"] for column in inspect(bind).get_columns("ticket_categories")}
        with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
            for name in (
                "excluded_user_field",
                "min_ladder_step",
                "min_tenure_days",
                "hidden",
                "info_text",
                "topic",
                "visible_ranks",
                "kind",
            ):
                if name in columns:
                    batch_op.drop_column(name)
