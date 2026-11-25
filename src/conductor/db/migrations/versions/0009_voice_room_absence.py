"""Owner absence timestamp for voice room claim.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05 02:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_absent_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("voice_rooms", schema=None) as batch_op:
        batch_op.drop_column("owner_absent_since")
