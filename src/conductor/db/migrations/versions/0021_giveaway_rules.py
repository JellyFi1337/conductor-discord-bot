"""Giveaway rules, ticket weights, and consolation places.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-05 05:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("giveaway_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("weight_messages", sa.Integer(), nullable=False, server_default="50"))
        batch_op.add_column(sa.Column("weight_voice_minutes", sa.Integer(), nullable=False, server_default="30"))
        batch_op.add_column(sa.Column("weight_max", sa.Integer(), nullable=False, server_default="10"))
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rules_json", sa.Text(), nullable=False, server_default="{}"))
    with op.batch_alter_table("giveaway_winners", schema=None) as batch_op:
        batch_op.add_column(sa.Column("place", sa.String(length=16), nullable=False, server_default="main"))


def downgrade() -> None:
    with op.batch_alter_table("giveaway_winners", schema=None) as batch_op:
        batch_op.drop_column("place")
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.drop_column("rules_json")
    with op.batch_alter_table("giveaway_settings", schema=None) as batch_op:
        batch_op.drop_column("weight_max")
        batch_op.drop_column("weight_voice_minutes")
        batch_op.drop_column("weight_messages")
