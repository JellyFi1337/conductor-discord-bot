"""Ticket log channel card and stored transcripts.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-07 04:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "tickets" in tables:
        columns = {column["name"] for column in inspect(bind).get_columns("tickets")}
        if "log_message_id" not in columns:
            with op.batch_alter_table("tickets", schema=None) as batch_op:
                batch_op.add_column(sa.Column("log_message_id", sa.BigInteger(), nullable=True))
    if "ticket_transcripts" not in tables:
        op.create_table(
            "ticket_transcripts",
            sa.Column("ticket_id", sa.Integer(), primary_key=True),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ticket_transcripts_guild_id", "ticket_transcripts", ["guild_id"])
        op.create_index("ix_ticket_transcripts_created_at", "ticket_transcripts", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "ticket_transcripts" in tables:
        indexes = {index["name"] for index in inspect(bind).get_indexes("ticket_transcripts")}
        if "ix_ticket_transcripts_created_at" in indexes:
            op.drop_index("ix_ticket_transcripts_created_at", table_name="ticket_transcripts")
        if "ix_ticket_transcripts_guild_id" in indexes:
            op.drop_index("ix_ticket_transcripts_guild_id", table_name="ticket_transcripts")
        op.drop_table("ticket_transcripts")
    if "tickets" not in tables:
        return
    columns = {column["name"] for column in inspect(bind).get_columns("tickets")}
    if "log_message_id" in columns:
        with op.batch_alter_table("tickets", schema=None) as batch_op:
            batch_op.drop_column("log_message_id")
