"""Reputation score, events, and guild coefficients.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-05 04:50:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reputation_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=False),
        sa.Column("ceiling", sa.Integer(), nullable=False),
        sa.Column("warn_hit", sa.Integer(), nullable=False),
        sa.Column("timeout_step_seconds", sa.Integer(), nullable=False),
        sa.Column("timeout_per_step", sa.Integer(), nullable=False),
        sa.Column("timeout_min", sa.Integer(), nullable=False),
        sa.Column("timeout_max", sa.Integer(), nullable=False),
        sa.Column("mute_hit", sa.Integer(), nullable=False),
        sa.Column("kick_hit", sa.Integer(), nullable=False),
        sa.Column("max_hit_per_hour", sa.Integer(), nullable=False),
        sa.Column("messages_per_point", sa.Integer(), nullable=False),
        sa.Column("voice_seconds_per_point", sa.Integer(), nullable=False),
        sa.Column("growth_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("sinner_threshold", sa.Integer(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_reputation_settings")),
    )
    op.create_table(
        "reputation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("credited_messages", sa.Integer(), nullable=False),
        sa.Column("credited_voice_seconds", sa.Integer(), nullable=False),
        sa.Column("last_growth_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reputation")),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_reputation_user"),
    )
    with op.batch_alter_table("reputation", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reputation_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reputation_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_reputation_guild_user", ["guild_id", "user_id"], unique=False)
    op.create_table(
        "reputation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("cut", sa.Integer(), nullable=False),
        sa.Column("score_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reputation_events")),
    )
    with op.batch_alter_table("reputation_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reputation_events_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_reputation_events_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            "ix_reputation_events_guild_user_created",
            ["guild_id", "user_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "uq_reputation_events_case",
            ["guild_id", "case_id"],
            unique=True,
            sqlite_where=sa.text("case_id IS NOT NULL"),
            postgresql_where=sa.text("case_id IS NOT NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("reputation_events", schema=None) as batch_op:
        batch_op.drop_index("uq_reputation_events_case")
        batch_op.drop_index("ix_reputation_events_guild_user_created")
        batch_op.drop_index(batch_op.f("ix_reputation_events_user_id"))
        batch_op.drop_index(batch_op.f("ix_reputation_events_guild_id"))
    op.drop_table("reputation_events")
    with op.batch_alter_table("reputation", schema=None) as batch_op:
        batch_op.drop_index("ix_reputation_guild_user")
        batch_op.drop_index(batch_op.f("ix_reputation_user_id"))
        batch_op.drop_index(batch_op.f("ix_reputation_guild_id"))
    op.drop_table("reputation")
    op.drop_table("reputation_settings")
