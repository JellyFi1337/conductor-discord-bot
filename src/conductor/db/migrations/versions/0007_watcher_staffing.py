"""Watcher duty, notes, and staffing policy flags.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05 02:31:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guild_access_states", schema=None) as batch_op:
        batch_op.add_column(sa.Column("senior_appoint", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("special_appoint", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "watcher_duty",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("set_by", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", name=op.f("pk_watcher_duty")),
    )
    op.create_table(
        "watcher_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watcher_notes")),
    )
    with op.batch_alter_table("watcher_notes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_watcher_notes_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_watcher_notes_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            "ix_watcher_notes_guild_user_created",
            ["guild_id", "user_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("watcher_notes", schema=None) as batch_op:
        batch_op.drop_index("ix_watcher_notes_guild_user_created")
        batch_op.drop_index(batch_op.f("ix_watcher_notes_user_id"))
        batch_op.drop_index(batch_op.f("ix_watcher_notes_guild_id"))
    op.drop_table("watcher_notes")
    op.drop_table("watcher_duty")
    with op.batch_alter_table("guild_access_states", schema=None) as batch_op:
        batch_op.drop_column("special_appoint")
        batch_op.drop_column("senior_appoint")
