"""Personal reminders with due_at surviving restart.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-05 07:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reminders_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reminders_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reminders_due_at"), ["due_at"], unique=False)
        batch_op.create_index("ix_reminders_guild_user_due", ["guild_id", "user_id", "due_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("reminders", schema=None) as batch_op:
        batch_op.drop_index("ix_reminders_guild_user_due")
        batch_op.drop_index(batch_op.f("ix_reminders_due_at"))
        batch_op.drop_index(batch_op.f("ix_reminders_user_id"))
        batch_op.drop_index(batch_op.f("ix_reminders_guild_id"))
    op.drop_table("reminders")
