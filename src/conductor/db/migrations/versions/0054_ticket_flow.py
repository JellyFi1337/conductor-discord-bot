"""Ticket approval and appeal journal.

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-07 03:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0054"
down_revision: str | Sequence[str] | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "ticket_events" in tables:
        return
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_events")),
    )
    with op.batch_alter_table("ticket_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ticket_events_ticket_id"), ["ticket_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ticket_events_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_ticket_events_ticket_created", ["ticket_id", "created_at"], unique=False)


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "ticket_events" not in tables:
        return
    with op.batch_alter_table("ticket_events", schema=None) as batch_op:
        batch_op.drop_index("ix_ticket_events_ticket_created")
        batch_op.drop_index(batch_op.f("ix_ticket_events_guild_id"))
        batch_op.drop_index(batch_op.f("ix_ticket_events_ticket_id"))
    op.drop_table("ticket_events")
