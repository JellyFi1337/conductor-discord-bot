"""Six reputation tiers per guild.

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-07 04:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0057"
down_revision: str | Sequence[str] | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOUNDS: tuple[tuple[int, int, int, str], ...] = (
    (0, 0, 10, "Изгой"),
    (1, 10, 30, "Подозрительный"),
    (2, 30, 50, "Обычный"),
    (3, 50, 70, "Надёжный"),
    (4, 70, 90, "Уважаемый"),
    (5, 90, 101, "Образцовый"),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "reputation_tiers" not in tables:
        op.create_table(
            "reputation_tiers",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("tier", sa.Integer(), nullable=False),
            sa.Column("lower", sa.Integer(), nullable=False),
            sa.Column("upper", sa.Integer(), nullable=False),
            sa.Column("role_id", sa.BigInteger(), nullable=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_reputation_tiers")),
            sa.UniqueConstraint("guild_id", "tier", name="uq_reputation_tiers_guild_tier"),
        )
        op.create_index("ix_reputation_tiers_guild_id", "reputation_tiers", ["guild_id"])
    if "reputation_settings" not in tables:
        return
    settings = sa.table("reputation_settings", sa.column("guild_id", sa.BigInteger()))
    tiers = sa.table(
        "reputation_tiers",
        sa.column("guild_id", sa.BigInteger()),
        sa.column("tier", sa.Integer()),
        sa.column("lower", sa.Integer()),
        sa.column("upper", sa.Integer()),
        sa.column("name", sa.String()),
    )
    rows = bind.execute(sa.select(settings.c.guild_id)).fetchall()
    existing = {
        (int(row.guild_id), int(row.tier)) for row in bind.execute(sa.select(tiers.c.guild_id, tiers.c.tier)).fetchall()
    }
    payload: list[dict[str, object]] = []
    for (guild_id,) in rows:
        gid = int(guild_id)
        for tier, lower, upper, name in _BOUNDS:
            if (gid, tier) in existing:
                continue
            payload.append(
                {
                    "guild_id": gid,
                    "tier": tier,
                    "lower": lower,
                    "upper": upper,
                    "name": name,
                }
            )
    if payload:
        op.bulk_insert(tiers, payload)


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "reputation_tiers" not in tables:
        return
    indexes = {index["name"] for index in inspect(bind).get_indexes("reputation_tiers")}
    if "ix_reputation_tiers_guild_id" in indexes:
        op.drop_index("ix_reputation_tiers_guild_id", table_name="reputation_tiers")
    op.drop_table("reputation_tiers")
