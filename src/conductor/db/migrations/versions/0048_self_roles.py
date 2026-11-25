"""Self-role menus and options.

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-07 02:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "self_role_menus" not in tables:
        op.create_table(
            "self_role_menus",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=80), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=True),
            sa.Column("panel_id", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_self_role_menus")),
        )
        op.create_index(op.f("ix_self_role_menus_guild_id"), "self_role_menus", ["guild_id"], unique=False)
    if "self_role_options" not in tables:
        op.create_table(
            "self_role_options",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("menu_id", sa.Integer(), nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("role_id", sa.BigInteger(), nullable=False),
            sa.Column("label", sa.String(length=80), nullable=False),
            sa.Column("description", sa.String(length=100), nullable=False),
            sa.Column("emoji", sa.String(length=64), nullable=True),
            sa.Column("tag", sa.String(length=16), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_self_role_options")),
            sa.UniqueConstraint("guild_id", "role_id", name="uq_self_role_option_role"),
        )
        op.create_index("ix_self_role_options_menu", "self_role_options", ["menu_id"], unique=False)
        op.create_index(op.f("ix_self_role_options_guild_id"), "self_role_options", ["guild_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "self_role_options" in tables:
        op.drop_index(op.f("ix_self_role_options_guild_id"), table_name="self_role_options")
        op.drop_index("ix_self_role_options_menu", table_name="self_role_options")
        op.drop_table("self_role_options")
    if "self_role_menus" in tables:
        op.drop_index(op.f("ix_self_role_menus_guild_id"), table_name="self_role_menus")
        op.drop_table("self_role_menus")
