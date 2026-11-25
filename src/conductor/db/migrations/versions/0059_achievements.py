"""Achievement defs, progress, grants, and ticket sources.

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-07 05:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0059"
down_revision: str | Sequence[str] | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED: tuple[tuple[str, int, int, str], ...] = (
    ("fixer", 1, 1, "Фиксер 1"),
    ("fixer", 2, 5, "Фиксер 2"),
    ("fixer", 3, 10, "Фиксер 3"),
    ("ideologist", 1, 1, "Идеолог 1"),
    ("ideologist", 2, 5, "Идеолог 2"),
    ("ideologist", 3, 10, "Идеолог 3"),
    ("vigilant", 1, 1, "Бдительный 1"),
    ("vigilant", 2, 5, "Бдительный 2"),
    ("vigilant", 3, 10, "Бдительный 3"),
    ("vigilant", 4, 20, "Бдительный 4"),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "achievement_defs" not in tables:
        op.create_table(
            "achievement_defs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("key", sa.String(length=16), nullable=False),
            sa.Column("stage", sa.Integer(), nullable=False),
            sa.Column("threshold", sa.Integer(), nullable=False),
            sa.Column("role_id", sa.BigInteger(), nullable=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_achievement_defs")),
            sa.UniqueConstraint("guild_id", "key", "stage", name="uq_achievement_defs_guild_key_stage"),
        )
        op.create_index("ix_achievement_defs_guild_id", "achievement_defs", ["guild_id"])
    if "achievement_progress" not in tables:
        op.create_table(
            "achievement_progress",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("key", sa.String(length=16), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_achievement_progress")),
            sa.UniqueConstraint("guild_id", "user_id", "key", name="uq_achievement_progress_user_key"),
        )
        op.create_index("ix_achievement_progress_guild_id", "achievement_progress", ["guild_id"])
        op.create_index("ix_achievement_progress_user_id", "achievement_progress", ["user_id"])
    if "achievement_grants" not in tables:
        op.create_table(
            "achievement_grants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("key", sa.String(length=16), nullable=False),
            sa.Column("stage", sa.Integer(), nullable=False),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_achievement_grants")),
            sa.UniqueConstraint("guild_id", "user_id", "key", "stage", name="uq_achievement_grants_user_stage"),
        )
        op.create_index("ix_achievement_grants_guild_id", "achievement_grants", ["guild_id"])
        op.create_index("ix_achievement_grants_user_id", "achievement_grants", ["user_id"])
    if "achievement_sources" not in tables:
        op.create_table(
            "achievement_sources",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("ticket_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("key", sa.String(length=16), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_achievement_sources")),
            sa.UniqueConstraint("guild_id", "ticket_id", name="uq_achievement_sources_ticket"),
        )
        op.create_index("ix_achievement_sources_guild_id", "achievement_sources", ["guild_id"])
    if "guild_configs" not in tables or "achievement_defs" not in inspect(bind).get_table_names():
        return
    guilds = sa.table("guild_configs", sa.column("guild_id", sa.BigInteger()))
    defs = sa.table(
        "achievement_defs",
        sa.column("guild_id", sa.BigInteger()),
        sa.column("key", sa.String()),
        sa.column("stage", sa.Integer()),
        sa.column("threshold", sa.Integer()),
        sa.column("name", sa.String()),
    )
    rows = bind.execute(sa.select(guilds.c.guild_id)).fetchall()
    existing = {
        (int(row.guild_id), str(row.key), int(row.stage))
        for row in bind.execute(sa.select(defs.c.guild_id, defs.c.key, defs.c.stage)).fetchall()
    }
    payload: list[dict[str, object]] = []
    for (guild_id,) in rows:
        gid = int(guild_id)
        for key, stage, threshold, name in _SEED:
            if (gid, key, stage) in existing:
                continue
            payload.append(
                {
                    "guild_id": gid,
                    "key": key,
                    "stage": stage,
                    "threshold": threshold,
                    "name": name,
                }
            )
    if payload:
        op.bulk_insert(defs, payload)


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    for name, index in (
        ("achievement_sources", "ix_achievement_sources_guild_id"),
        ("achievement_grants", "ix_achievement_grants_guild_id"),
        ("achievement_progress", "ix_achievement_progress_guild_id"),
        ("achievement_defs", "ix_achievement_defs_guild_id"),
    ):
        if name not in tables:
            continue
        indexes = {item["name"] for item in inspect(bind).get_indexes(name)}
        if index in indexes:
            op.drop_index(index, table_name=name)
        if name == "achievement_grants" and "ix_achievement_grants_user_id" in indexes:
            op.drop_index("ix_achievement_grants_user_id", table_name=name)
        if name == "achievement_progress" and "ix_achievement_progress_user_id" in indexes:
            op.drop_index("ix_achievement_progress_user_id", table_name=name)
        op.drop_table(name)
