"""Evidence channels and DM tracking for moderation cases.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-07 00:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("mod_settings")}
    with op.batch_alter_table("mod_settings", schema=None) as batch_op:
        if "evidence_category_id" not in columns:
            batch_op.add_column(sa.Column("evidence_category_id", sa.BigInteger(), nullable=True))
        if "evidence_archive_channel_id" not in columns:
            batch_op.add_column(sa.Column("evidence_archive_channel_id", sa.BigInteger(), nullable=True))
        if "evidence_remind_hours" not in columns:
            batch_op.add_column(sa.Column("evidence_remind_hours", sa.Integer(), nullable=False, server_default="24"))
    tables = set(inspect(op.get_bind()).get_table_names())
    if "mod_evidence" in tables:
        return
    op.create_table(
        "mod_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_reason", sa.Text(), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("dm1_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dm2_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mod_evidence")),
        sa.UniqueConstraint("case_id", name="uq_mod_evidence_case"),
    )
    with op.batch_alter_table("mod_evidence", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mod_evidence_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index("ix_mod_evidence_guild_created", ["guild_id", "created_at"], unique=False)


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "mod_evidence" in tables:
        with op.batch_alter_table("mod_evidence", schema=None) as batch_op:
            batch_op.drop_index("ix_mod_evidence_guild_created")
            batch_op.drop_index(batch_op.f("ix_mod_evidence_guild_id"))
        op.drop_table("mod_evidence")
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("mod_settings")}
    with op.batch_alter_table("mod_settings", schema=None) as batch_op:
        if "evidence_remind_hours" in columns:
            batch_op.drop_column("evidence_remind_hours")
        if "evidence_archive_channel_id" in columns:
            batch_op.drop_column("evidence_archive_channel_id")
        if "evidence_category_id" in columns:
            batch_op.drop_column("evidence_category_id")
