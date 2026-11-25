"""Ticket panels, categories, and tickets.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-05 03:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_panels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_panels")),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_ticket_panel_channel"),
    )
    op.create_table(
        "ticket_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_categories")),
        sa.UniqueConstraint("panel_id", "name", name="uq_ticket_category_name"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.BigInteger(), nullable=True),
        sa.Column("claimed_rank", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("guild_id", "number", name="uq_tickets_number"),
    )
    with op.batch_alter_table("ticket_panels", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ticket_panels_guild_id"), ["guild_id"], unique=False)
    with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ticket_categories_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_ticket_categories_panel", ["panel_id"], unique=False)
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tickets_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tickets_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tickets_panel_id"), ["panel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tickets_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index("ix_tickets_guild_author_state", ["guild_id", "owner_id", "state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.drop_index("ix_tickets_guild_author_state")
        batch_op.drop_index(batch_op.f("ix_tickets_channel_id"))
        batch_op.drop_index(batch_op.f("ix_tickets_panel_id"))
        batch_op.drop_index(batch_op.f("ix_tickets_owner_id"))
        batch_op.drop_index(batch_op.f("ix_tickets_guild_id"))
    with op.batch_alter_table("ticket_categories", schema=None) as batch_op:
        batch_op.drop_index("ix_ticket_categories_panel")
        batch_op.drop_index(batch_op.f("ix_ticket_categories_guild_id"))
    with op.batch_alter_table("ticket_panels", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ticket_panels_guild_id"))
    op.drop_table("tickets")
    op.drop_table("ticket_categories")
    op.drop_table("ticket_panels")
