from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GuildConfig, LoyaltyRegistry, LoyaltyRoleEvent
from conductor.domain.loyalty import (
    TIER_PRIORITY,
    LoyaltyTier,
    configured_old_roles,
    registry_delta,
    snapshot_tier,
)
from conductor.services.guild_config import role_id_for


def old_roles_from_config(config: GuildConfig | None) -> dict[LoyaltyTier, int]:
    if config is None:
        return {}
    return configured_old_roles(
        {
            LoyaltyTier.RUBY: config.ruby_role_id,
            LoyaltyTier.EMERALD: config.emerald_role_id,
            LoyaltyTier.SAPPHIRE: config.sapphire_role_id,
            LoyaltyTier.AMETHYST: config.amethyst_role_id,
        }
    )


def old_role_id_set(config: GuildConfig | None) -> set[int]:
    return set(old_roles_from_config(config).values())


async def load_registry(session: AsyncSession, guild_id: int) -> dict[int, LoyaltyTier]:
    rows = (await session.scalars(select(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id))).all()
    return {int(row.user_id): LoyaltyTier(row.tier) for row in rows}


async def registry_counts(session: AsyncSession, guild_id: int) -> dict[LoyaltyTier, int]:
    counts = dict.fromkeys(TIER_PRIORITY, 0)
    for tier in (await load_registry(session, guild_id)).values():
        counts[tier] += 1
    return counts


def authorized_role_id(
    config: GuildConfig,
    registry: dict[int, LoyaltyTier],
    user_id: int,
) -> int | None:
    tier = registry.get(user_id)
    if tier is None:
        return None
    return role_id_for(config, tier)


def count_registry_mismatches(
    members: list[tuple[int, set[int], bool]],
    config: GuildConfig,
    registry: dict[int, LoyaltyTier],
) -> int:
    old_ids = old_role_id_set(config)
    if not old_ids:
        return 0
    mismatches = 0
    for user_id, role_ids, is_bot in members:
        if is_bot:
            continue
        delta = registry_delta(
            role_ids,
            authorized_role_id=authorized_role_id(config, registry, user_id),
            old_role_ids=old_ids,
        )
        if not delta.empty:
            mismatches += 1
    return mismatches


async def seed_registry_from_guild(
    session: AsyncSession,
    *,
    guild_id: int,
    members: list[tuple[int, set[int], bool]],
    config: GuildConfig,
) -> int:
    if config.loyalty_registry_seeded_at is not None:
        return 0
    existing = await session.scalar(
        select(func.count()).select_from(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id)
    )
    if int(existing or 0) > 0:
        return 0
    old_roles = old_roles_from_config(config)
    if not old_roles:
        return 0
    now = datetime.now(UTC)
    added = 0
    for user_id, role_ids, is_bot in members:
        if is_bot:
            continue
        tier = snapshot_tier(role_ids, old_roles)
        if tier is None:
            continue
        session.add(
            LoyaltyRegistry(
                guild_id=guild_id,
                user_id=user_id,
                tier=tier.value,
                updated_at=now,
            )
        )
        added += 1
    if added:
        config.loyalty_registry_seeded_at = now
    return added


async def upsert_registry_members(
    session: AsyncSession,
    *,
    guild_id: int,
    assignments: dict[int, LoyaltyTier],
    config: GuildConfig | None = None,
) -> tuple[int, int]:
    now = datetime.now(UTC)
    rows = (await session.scalars(select(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id))).all()
    by_user = {int(row.user_id): row for row in rows}
    added = 0
    updated = 0
    for user_id, tier in assignments.items():
        row = by_user.get(int(user_id))
        if row is None:
            session.add(
                LoyaltyRegistry(
                    guild_id=guild_id,
                    user_id=int(user_id),
                    tier=tier.value,
                    updated_at=now,
                )
            )
            added += 1
            continue
        if row.tier != tier.value:
            row.tier = tier.value
            row.updated_at = now
            updated += 1
    if (added or updated) and config is not None and config.loyalty_registry_seeded_at is None:
        config.loyalty_registry_seeded_at = now
    return added, updated


async def drop_registry_users(
    session: AsyncSession,
    *,
    guild_id: int,
    user_ids: Iterable[int],
) -> int:
    wanted = tuple(int(user_id) for user_id in user_ids)
    if not wanted:
        return 0
    result = await session.execute(
        delete(LoyaltyRegistry).where(
            LoyaltyRegistry.guild_id == guild_id,
            LoyaltyRegistry.user_id.in_(wanted),
        )
    )
    return int(result.rowcount or 0)


async def replace_registry(
    session: AsyncSession,
    *,
    guild_id: int,
    assignments: dict[int, LoyaltyTier],
) -> None:
    await session.execute(delete(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id))
    now = datetime.now(UTC)
    for user_id, tier in assignments.items():
        session.add(
            LoyaltyRegistry(
                guild_id=guild_id,
                user_id=user_id,
                tier=tier.value,
                updated_at=now,
            )
        )


async def record_role_event(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    actor_id: int | None,
    role_id: int,
    action: str,
    reverted: bool,
) -> None:
    session.add(
        LoyaltyRoleEvent(
            guild_id=guild_id,
            user_id=user_id,
            actor_id=actor_id,
            role_id=role_id,
            action=action,
            reverted=reverted,
            created_at=datetime.now(UTC),
        )
    )
