"""Birthdays, AFK, welcome, weekly challenge.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-05 07:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "birthday_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_birthday_settings")),
    )
    op.create_table(
        "birthdays",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("last_announced_year", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_birthdays")),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_birthday_person"),
    )
    with op.batch_alter_table("birthdays", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_birthdays_guild_id"), ["guild_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_birthdays_user_id"), ["user_id"], unique=False)
    op.create_table(
        "afk_status",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", name=op.f("pk_afk_status")),
    )
    op.create_table(
        "welcome_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default="Добро пожаловать, {user}."),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_welcome_settings")),
    )
    op.create_table(
        "weekly_challenges",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_posted_week", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_weekly_challenges")),
    )


def downgrade() -> None:
    op.drop_table("weekly_challenges")
    op.drop_table("welcome_settings")
    op.drop_table("afk_status")
    with op.batch_alter_table("birthdays", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_birthdays_user_id"))
        batch_op.drop_index(batch_op.f("ix_birthdays_guild_id"))
    op.drop_table("birthdays")
    op.drop_table("birthday_settings")
