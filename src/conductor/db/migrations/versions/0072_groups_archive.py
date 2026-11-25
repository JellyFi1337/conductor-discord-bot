"""Group archive shelves, retention, invites and create limits.

Revision ID: 0072
Revises: 0071
Create Date: 2026-09-18 19:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0072"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    hub_cols = {column["name"] for column in inspector.get_columns("group_hubs")} if "group_hubs" in tables else set()
    group_cols = {column["name"] for column in inspector.get_columns("groups")} if "groups" in tables else set()
    if "archive_category_id" not in hub_cols:
        op.add_column("group_hubs", sa.Column("archive_category_id", sa.BigInteger(), nullable=True))
    if "archive_days" not in hub_cols:
        op.add_column(
            "group_hubs",
            sa.Column("archive_days", sa.Integer(), nullable=False, server_default="30"),
        )
    if "min_reputation" not in hub_cols:
        op.add_column(
            "group_hubs",
            sa.Column("min_reputation", sa.Integer(), nullable=False, server_default="50"),
        )
    if "daily_limit" not in hub_cols:
        op.add_column(
            "group_hubs",
            sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="1"),
        )
    if "open_limit" not in hub_cols:
        op.add_column(
            "group_hubs",
            sa.Column("open_limit", sa.Integer(), nullable=False, server_default="3"),
        )
    if "archive_reason" not in group_cols:
        op.add_column("groups", sa.Column("archive_reason", sa.String(length=16), nullable=True))
    if "purge_after" not in group_cols:
        op.add_column("groups", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    if "silence_warned_at" not in group_cols:
        op.add_column("groups", sa.Column("silence_warned_at", sa.DateTime(timezone=True), nullable=True))
    if "group_archive_shelves" not in tables:
        op.create_table(
            "group_archive_shelves",
            sa.Column("category_id", sa.BigInteger(), nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("category_id", name=op.f("pk_group_archive_shelves")),
        )
        op.create_index(op.f("ix_group_archive_shelves_guild_id"), "group_archive_shelves", ["guild_id"], unique=False)
    if "group_invites" not in tables:
        op.create_table(
            "group_invites",
            sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("invited_by", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=12), nullable=False),
            sa.Column("dm_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["group_id"],
                ["groups.id"],
                name=op.f("fk_group_invites_group_id_groups"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_group_invites")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "group_invites" in tables:
        op.drop_table("group_invites")
    if "group_archive_shelves" in tables:
        op.drop_index(op.f("ix_group_archive_shelves_guild_id"), table_name="group_archive_shelves")
        op.drop_table("group_archive_shelves")
    group_cols = {column["name"] for column in inspector.get_columns("groups")} if "groups" in tables else set()
    hub_cols = {column["name"] for column in inspector.get_columns("group_hubs")} if "group_hubs" in tables else set()
    if "silence_warned_at" in group_cols:
        op.drop_column("groups", "silence_warned_at")
    if "purge_after" in group_cols:
        op.drop_column("groups", "purge_after")
    if "archive_reason" in group_cols:
        op.drop_column("groups", "archive_reason")
    if "open_limit" in hub_cols:
        op.drop_column("group_hubs", "open_limit")
    if "daily_limit" in hub_cols:
        op.drop_column("group_hubs", "daily_limit")
    if "min_reputation" in hub_cols:
        op.drop_column("group_hubs", "min_reputation")
    if "archive_days" in hub_cols:
        op.drop_column("group_hubs", "archive_days")
    if "archive_category_id" in hub_cols:
        op.drop_column("group_hubs", "archive_category_id")
