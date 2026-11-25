"""Named invite campaigns for growth attribution.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-05 11:52:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invite_campaigns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_campaigns")),
        sa.UniqueConstraint("guild_id", "name", name="uq_invite_campaign_name"),
        sa.UniqueConstraint("guild_id", "code", name="uq_invite_campaign_code"),
    )
    with op.batch_alter_table("invite_campaigns", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invite_campaigns_guild_id"), ["guild_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("invite_campaigns", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invite_campaigns_guild_id"))
    op.drop_table("invite_campaigns")
