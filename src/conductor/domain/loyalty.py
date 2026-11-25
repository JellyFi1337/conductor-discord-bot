from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class LoyaltyTier(StrEnum):
    RUBY = "ruby_old"
    EMERALD = "emerald_old"
    SAPPHIRE = "sapphire_old"
    AMETHYST = "amethyst_old"


TIER_LABELS: dict[LoyaltyTier, str] = {
    LoyaltyTier.RUBY: "Ruby Old",
    LoyaltyTier.EMERALD: "Emerald Old",
    LoyaltyTier.SAPPHIRE: "Sapphire Old",
    LoyaltyTier.AMETHYST: "Amethyst Old",
}

TIER_PRIORITY: tuple[LoyaltyTier, ...] = (
    LoyaltyTier.RUBY,
    LoyaltyTier.EMERALD,
    LoyaltyTier.SAPPHIRE,
    LoyaltyTier.AMETHYST,
)

MIN_MESSAGES_FOR_LOYALTY = 10


class LoyaltySkip(StrEnum):
    BOT = "bot"
    TODAY = "today"
    SILENT = "silent"


def qualifies_by_messages(message_count: int) -> bool:
    return message_count >= MIN_MESSAGES_FOR_LOYALTY


def loyalty_threshold_copy() -> str:
    """Same rule as qualifies_by_messages: at least N messages, not 'more than N'."""
    return f"не менее {MIN_MESSAGES_FOR_LOYALTY} сообщений"


def below_threshold_label() -> str:
    return f"Меньше {MIN_MESSAGES_FOR_LOYALTY} сообщений"


@dataclass(frozen=True, slots=True)
class LoyaltyVerdict:
    skip: LoyaltySkip | None
    tier: LoyaltyTier | None


def classify_member(
    *,
    is_bot: bool,
    message_count: int,
    joined_at: datetime,
    guild_created_at: datetime,
    now: datetime | None = None,
) -> LoyaltyVerdict:
    if is_bot:
        return LoyaltyVerdict(LoyaltySkip.BOT, None)
    tier = resolve_tier(guild_created_at=guild_created_at, joined_at=joined_at, now=now)
    if tier is None:
        return LoyaltyVerdict(LoyaltySkip.TODAY, None)
    if not qualifies_by_messages(message_count):
        return LoyaltyVerdict(LoyaltySkip.SILENT, None)
    return LoyaltyVerdict(None, tier)


def add_calendar_months(moment: datetime, months: int) -> datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def add_calendar_years(moment: datetime, years: int) -> datetime:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, month=2, day=28)


def resolve_tier(
    *,
    guild_created_at: datetime,
    joined_at: datetime,
    now: datetime | None = None,
) -> LoyaltyTier | None:
    """Pick exactly one loyalty tier, or None if the member joined today.

    Priority (exclusive — a member never receives two of these roles):
    1. Ruby Old — joined during the first 3 months of the guild
    2. Emerald Old — tenure of at least 2 years
    3. Sapphire Old — tenure of at least 1 year
    4. Amethyst Old — joined before today
    """
    now = now or datetime.now(UTC)
    joined_at = _aware(joined_at)
    guild_created_at = _aware(guild_created_at)
    now = _aware(now)

    if joined_at.date() >= now.date():
        return None

    ruby_deadline = add_calendar_months(guild_created_at, 3)
    if joined_at <= ruby_deadline:
        return LoyaltyTier.RUBY

    if joined_at <= add_calendar_years(now, -2):
        return LoyaltyTier.EMERALD

    if joined_at <= add_calendar_years(now, -1):
        return LoyaltyTier.SAPPHIRE

    return LoyaltyTier.AMETHYST


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RoleDelta:
    add_ids: tuple[int, ...]
    remove_ids: tuple[int, ...]

    @property
    def empty(self) -> bool:
        return not self.add_ids and not self.remove_ids


def configured_old_roles(role_ids: dict[LoyaltyTier, int | None]) -> dict[LoyaltyTier, int]:
    return {tier: role_id for tier, role_id in role_ids.items() if role_id}


def snapshot_tier(held_role_ids: set[int], old_roles: dict[LoyaltyTier, int]) -> LoyaltyTier | None:
    for tier in TIER_PRIORITY:
        role_id = old_roles.get(tier)
        if role_id is not None and role_id in held_role_ids:
            return tier
    return None


def registry_delta(
    held_role_ids: set[int],
    *,
    authorized_role_id: int | None,
    old_role_ids: set[int],
) -> RoleDelta:
    held_old = {role_id for role_id in held_role_ids if role_id in old_role_ids}
    authorized = {authorized_role_id} if authorized_role_id is not None else set()
    return RoleDelta(
        add_ids=tuple(sorted(authorized - held_old)),
        remove_ids=tuple(sorted(held_old - authorized)),
    )


def old_role_change(
    before_ids: set[int],
    after_ids: set[int],
    old_role_ids: set[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    before_old = before_ids & old_role_ids
    after_old = after_ids & old_role_ids
    granted = tuple(sorted(after_old - before_old))
    removed = tuple(sorted(before_old - after_old))
    return granted, removed
