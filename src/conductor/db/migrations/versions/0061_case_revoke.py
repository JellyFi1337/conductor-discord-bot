"""Case revoke fields and reputation revoke events.

Revision ID: 0061
Revises: 0060
Create Date: 2026-09-07 05:55:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0061"
down_revision: str | Sequence[str] | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    case_columns = {column["name"] for column in inspect(bind).get_columns("mod_cases")}
    if "revoked_at" not in case_columns:
        op.add_column("mod_cases", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    if "revoked_by" not in case_columns:
        op.add_column("mod_cases", sa.Column("revoked_by", sa.BigInteger(), nullable=True))
    if "revoke_reason" not in case_columns:
        op.add_column("mod_cases", sa.Column("revoke_reason", sa.Text(), nullable=True))
    if "appeal_ticket_id" not in case_columns:
        op.add_column("mod_cases", sa.Column("appeal_ticket_id", sa.Integer(), nullable=True))
    indexes = {item["name"] for item in inspect(bind).get_indexes("reputation_events")}
    if "uq_reputation_events_case" in indexes:
        with op.batch_alter_table("reputation_events", schema=None) as batch_op:
            batch_op.drop_index("uq_reputation_events_case")
            batch_op.create_index(
                "uq_reputation_events_case_kind",
                ["guild_id", "case_id", "kind"],
                unique=True,
                sqlite_where=sa.text("case_id IS NOT NULL"),
                postgresql_where=sa.text("case_id IS NOT NULL"),
            )
    elif "uq_reputation_events_case_kind" not in indexes:
        with op.batch_alter_table("reputation_events", schema=None) as batch_op:
            batch_op.create_index(
                "uq_reputation_events_case_kind",
                ["guild_id", "case_id", "kind"],
                unique=True,
                sqlite_where=sa.text("case_id IS NOT NULL"),
                postgresql_where=sa.text("case_id IS NOT NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in inspect(bind).get_indexes("reputation_events")}
    if "uq_reputation_events_case_kind" in indexes:
        with op.batch_alter_table("reputation_events", schema=None) as batch_op:
            batch_op.drop_index("uq_reputation_events_case_kind")
            batch_op.create_index(
                "uq_reputation_events_case",
                ["guild_id", "case_id"],
                unique=True,
                sqlite_where=sa.text("case_id IS NOT NULL"),
                postgresql_where=sa.text("case_id IS NOT NULL"),
            )
    case_columns = {column["name"] for column in inspect(bind).get_columns("mod_cases")}
    with op.batch_alter_table("mod_cases", schema=None) as batch_op:
        for name in ("appeal_ticket_id", "revoke_reason", "revoked_by", "revoked_at"):
            if name in case_columns:
                batch_op.drop_column(name)
