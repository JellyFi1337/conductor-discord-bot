"""Sinner overlay, reputation autoban flag, appeal contact.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-05 05:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reputation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("banned_for_reputation", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("reputation_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sinner_role_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "appeal_contact",
                sa.Text(),
                nullable=False,
                server_default="напишите администрации в личные сообщения",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("reputation_settings", schema=None) as batch_op:
        batch_op.drop_column("appeal_contact")
        batch_op.drop_column("sinner_role_id")
    with op.batch_alter_table("reputation", schema=None) as batch_op:
        batch_op.drop_column("banned_for_reputation")
