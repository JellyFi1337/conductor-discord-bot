"""Persistent guild showcase.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-05 11:14:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "showcases",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("message_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("highlights", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("invite_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bar_hash", sa.String(length=64), nullable=True),
        sa.Column("bar_edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_showcases")),
    )


def downgrade() -> None:
    op.drop_table("showcases")
