"""Drop showcase and pulse dock tables.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-06 23:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pulse_docks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pulse_docks_guild_id"))
    op.drop_table("pulse_docks")
    op.drop_table("showcases")


def downgrade() -> None:
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
    op.create_table(
        "pulse_docks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("template", sa.String(length=200), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("last_renamed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pulse_docks")),
        sa.UniqueConstraint("channel_id", name="uq_pulse_dock_channel"),
    )
    with op.batch_alter_table("pulse_docks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_pulse_docks_guild_id"), ["guild_id"], unique=False)
