"""Verification trap channel and join-spike floor.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-07 02:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("pulse_alert_settings")}
    if "join_spike_floor" not in columns:
        op.add_column(
            "pulse_alert_settings",
            sa.Column("join_spike_floor", sa.Integer(), nullable=False, server_default="15"),
        )
    if "verification_settings" in tables:
        return
    op.create_table(
        "verification_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("verified_role_id", sa.BigInteger(), nullable=True),
        sa.Column("panel_id", sa.Integer(), nullable=True),
        sa.Column("warning_message_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_verification_settings")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    columns = {column["name"] for column in inspect(bind).get_columns("pulse_alert_settings")}
    if "verification_settings" in tables:
        op.drop_table("verification_settings")
    if "join_spike_floor" in columns:
        with op.batch_alter_table("pulse_alert_settings", schema=None) as batch_op:
            batch_op.drop_column("join_spike_floor")
