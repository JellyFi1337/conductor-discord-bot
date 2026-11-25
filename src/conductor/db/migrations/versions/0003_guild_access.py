"""Guild rank and permission tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05 01:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_ranks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.String(length=16), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guild_ranks")),
        sa.UniqueConstraint("guild_id", "rank", "role_id", name="uq_guild_rank_role"),
    )
    with op.batch_alter_table("guild_ranks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_guild_ranks_guild_id"), ["guild_id"], unique=False)

    op.create_table(
        "guild_permissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("ranks", sa.Text(), nullable=False),
        sa.Column("extra_role_ids", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guild_permissions")),
        sa.UniqueConstraint("guild_id", "key", name="uq_guild_permission"),
    )
    with op.batch_alter_table("guild_permissions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_guild_permissions_guild_id"), ["guild_id"], unique=False)

    op.create_table(
        "guild_access_states",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("special_locked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_guild_access_states")),
    )


def downgrade() -> None:
    op.drop_table("guild_access_states")
    with op.batch_alter_table("guild_permissions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_guild_permissions_guild_id"))
    op.drop_table("guild_permissions")
    with op.batch_alter_table("guild_ranks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_guild_ranks_guild_id"))
    op.drop_table("guild_ranks")
