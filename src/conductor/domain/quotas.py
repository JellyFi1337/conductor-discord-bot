"""Дневные квоты следящих. Чистые функции: без Discord и без БД."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from conductor.domain.access import STAFF_RANKS, Rank
from conductor.domain.moderation import CaseKind

UNLIMITED = -1
QUOTA_MAX = 999

TIMEOUT_KINDS = frozenset({CaseKind.TIMEOUT, CaseKind.MUTE})
BAN_KINDS = frozenset({CaseKind.BAN, CaseKind.KICK})
COUNTED_KINDS = TIMEOUT_KINDS | BAN_KINDS


class QuotaGroup(StrEnum):
    TIMEOUTS = "timeouts"
    BANS = "bans"


@dataclass(frozen=True, slots=True)
class RankLimit:
    timeouts: int
    bans: int

    def for_group(self, group: QuotaGroup | str) -> int:
        return self.timeouts if QuotaGroup(group) is QuotaGroup.TIMEOUTS else self.bans


@dataclass(frozen=True, slots=True)
class QuotaTable:
    limits: Mapping[Rank, RankLimit]

    def for_rank(self, rank: Rank) -> RankLimit:
        found = self.limits.get(rank)
        if found is not None:
            return found
        return RankLimit(0, 0)


@dataclass(frozen=True, slots=True)
class QuotaUsed:
    timeouts: int = 0
    bans: int = 0

    def for_group(self, group: QuotaGroup | str) -> int:
        return self.timeouts if QuotaGroup(group) is QuotaGroup.TIMEOUTS else self.bans


@dataclass(frozen=True, slots=True)
class QuotaAllowed:
    pass


@dataclass(frozen=True, slots=True)
class QuotaDenied:
    limit: int
    used: int
    resets_at: datetime
    group: str


ALLOWED = QuotaAllowed()

DEFAULT_LIMITS: dict[Rank, RankLimit] = {
    Rank.WATCHER: RankLimit(10, 0),
    Rank.SENIOR: RankLimit(20, 1),
    Rank.CHIEF: RankLimit(40, 3),
    Rank.SPECIAL: RankLimit(UNLIMITED, UNLIMITED),
}

DEFAULT_TABLE = QuotaTable(DEFAULT_LIMITS)


def group_of(kind: CaseKind | str) -> QuotaGroup | None:
    try:
        resolved = CaseKind(kind)
    except ValueError:
        return None
    if resolved in TIMEOUT_KINDS:
        return QuotaGroup.TIMEOUTS
    if resolved in BAN_KINDS:
        return QuotaGroup.BANS
    return None


def resolve_zone(tz: str | ZoneInfo) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except (ModuleNotFoundError, ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC")


def _aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def day_start(now: datetime, tz: str | ZoneInfo) -> datetime:
    zone = resolve_zone(tz)
    local = _aware(now).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def day_reset(now: datetime, tz: str | ZoneInfo) -> datetime:
    return day_start(now, tz) + timedelta(days=1)


def parse_limit(value: object, *, fallback: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if number == UNLIMITED:
        return UNLIMITED
    if 0 <= number <= QUOTA_MAX:
        return number
    return fallback


def parse_quota_pair(raw: str) -> tuple[int, int] | None:
    parts = str(raw or "").replace("−", "-").replace(",", " ").replace(";", " ").split()
    if len(parts) != 2:
        return None
    timeouts = parse_limit(parts[0], fallback=QUOTA_MAX + 1)
    bans = parse_limit(parts[1], fallback=QUOTA_MAX + 1)
    if timeouts > QUOTA_MAX or bans > QUOTA_MAX:
        return None
    return timeouts, bans


def parse_quota_json(raw: str | None) -> QuotaTable:
    limits = dict(DEFAULT_LIMITS)
    if not raw or not str(raw).strip():
        return QuotaTable(limits)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return QuotaTable(limits)
    if not isinstance(payload, dict):
        return QuotaTable(limits)
    for name, values in payload.items():
        try:
            rank = Rank[str(name).upper()]
        except KeyError:
            continue
        if rank not in STAFF_RANKS or not isinstance(values, dict):
            continue
        current = limits[rank]
        limits[rank] = RankLimit(
            parse_limit(values.get("timeouts"), fallback=current.timeouts),
            parse_limit(values.get("bans"), fallback=current.bans),
        )
    return QuotaTable(limits)


def dump_quota_json(table: QuotaTable) -> str:
    payload = {}
    for rank in STAFF_RANKS:
        limit = table.for_rank(rank)
        payload[rank.key] = {"timeouts": limit.timeouts, "bans": limit.bans}
    return json.dumps(payload, ensure_ascii=False)


def quota_verdict(
    rank: Rank,
    used: int,
    table: QuotaTable,
    kind: CaseKind | str,
    bypass: bool,
    *,
    now: datetime,
    tz: str | ZoneInfo,
) -> QuotaAllowed | QuotaDenied:
    group = group_of(kind)
    if group is None:
        return ALLOWED
    resets_at = day_reset(now, tz)
    if bypass:
        return ALLOWED
    limit = table.for_rank(rank).for_group(group)
    if limit == UNLIMITED or used < limit:
        return ALLOWED
    return QuotaDenied(limit=limit, used=used, resets_at=resets_at, group=group.value)


def at_daily_limit(rank: Rank, used: QuotaUsed, table: QuotaTable) -> bool:
    limits = table.for_rank(rank)
    if limits.timeouts >= 0 and used.timeouts > 0 and used.timeouts >= limits.timeouts:
        return True
    return limits.bans >= 0 and used.bans > 0 and used.bans >= limits.bans
