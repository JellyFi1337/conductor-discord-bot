from __future__ import annotations

from datetime import UTC
from datetime import tzinfo as TzInfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GuildConfig
from conductor.domain.loyalty import LoyaltyTier

_TZ: dict[int, TzInfo] = {}


def _fallback_tz() -> TzInfo:
    try:
        return ZoneInfo("UTC")
    except (ModuleNotFoundError, ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def remember_tz(guild_id: int, name: str | None) -> TzInfo:
    zone = _fallback_tz()
    if name:
        try:
            zone = ZoneInfo(str(name))
        except (ModuleNotFoundError, ZoneInfoNotFoundError, ValueError, OSError):
            zone = _fallback_tz()
    _TZ[int(guild_id)] = zone
    return zone


def tz_for(guild_id: int) -> TzInfo:
    return _TZ.get(int(guild_id), _fallback_tz())


def forget_tz(guild_id: int | None = None) -> None:
    if guild_id is None:
        _TZ.clear()
        return
    _TZ.pop(int(guild_id), None)


async def get_or_create_config(session: AsyncSession, guild_id: int) -> GuildConfig:
    config = await session.get(GuildConfig, guild_id)
    if config is None:
        config = GuildConfig(guild_id=guild_id)
        session.add(config)
        await session.flush()
    return config


def role_id_for(config: GuildConfig, tier: LoyaltyTier) -> int | None:
    mapping = {
        LoyaltyTier.RUBY: config.ruby_role_id,
        LoyaltyTier.EMERALD: config.emerald_role_id,
        LoyaltyTier.SAPPHIRE: config.sapphire_role_id,
        LoyaltyTier.AMETHYST: config.amethyst_role_id,
    }
    return mapping[tier]


def set_role_id(config: GuildConfig, tier: LoyaltyTier, role_id: int) -> None:
    if tier is LoyaltyTier.RUBY:
        config.ruby_role_id = role_id
    elif tier is LoyaltyTier.EMERALD:
        config.emerald_role_id = role_id
    elif tier is LoyaltyTier.SAPPHIRE:
        config.sapphire_role_id = role_id
    else:
        config.amethyst_role_id = role_id


async def load_config(session: AsyncSession, guild_id: int) -> GuildConfig | None:
    return await session.scalar(select(GuildConfig).where(GuildConfig.guild_id == guild_id))
