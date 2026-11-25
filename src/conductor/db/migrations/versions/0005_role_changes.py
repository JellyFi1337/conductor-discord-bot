"""Role change journal.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05 02:04:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "role_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("subsystem", sa.String(length=16), nullable=False),
        sa.Column("add_role_ids", sa.Text(), nullable=False),
        sa.Column("remove_role_ids", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_changes")),
    )
    with op.batch_alter_table("role_changes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_role_changes_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_role_changes_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_role_changes_guild_user", ["guild_id", "user_id"], unique=False)
        batch_op.create_index("ix_role_changes_guild_created", ["guild_id", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("role_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_role_changes_guild_created")
        batch_op.drop_index("ix_role_changes_guild_user")
        batch_op.drop_index(batch_op.f("ix_role_changes_user_id"))
        batch_op.drop_index(batch_op.f("ix_role_changes_guild_id"))
    op.drop_table("role_changes")
