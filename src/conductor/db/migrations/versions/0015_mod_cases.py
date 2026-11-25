"""Moderation cases and mute role setting.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-05 04:25:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mod_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("mute_role_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_mod_settings")),
    )
    op.create_table(
        "mod_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("actor_rank", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mod_cases")),
        sa.UniqueConstraint("guild_id", "number", name="uq_mod_cases_number"),
    )
    with op.batch_alter_table("mod_cases", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mod_cases_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_mod_cases_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index("ix_mod_cases_guild_target", ["guild_id", "owner_id"], unique=False)
        batch_op.create_index(
            "ix_mod_cases_guild_actor_created", ["guild_id", "created_by", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("mod_cases", schema=None) as batch_op:
        batch_op.drop_index("ix_mod_cases_guild_actor_created")
        batch_op.drop_index("ix_mod_cases_guild_target")
        batch_op.drop_index(batch_op.f("ix_mod_cases_owner_id"))
        batch_op.drop_index(batch_op.f("ix_mod_cases_guild_id"))
    op.drop_table("mod_cases")
    op.drop_table("mod_settings")
