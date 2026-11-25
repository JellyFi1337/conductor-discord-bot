"""Ловушка верификации и воронка новичков. Без Discord и без БД."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.access import STAFF_RANKS, Rank, bypasses_limits
from conductor.domain.activity import as_utc
from conductor.domain.observe import JOIN_SPIKE_INTERVAL, should_emit

__all__ = [
    "JOIN_SPIKE_FLOOR",
    "JOIN_SPIKE_WINDOW",
    "TRAP_REASON",
    "FunnelCounts",
    "TrapVerdict",
    "everyone_can_send",
    "join_spike_due",
    "trap_verdict",
]

TRAP_REASON = "сообщение в канале верификации"
JOIN_SPIKE_FLOOR = 15
JOIN_SPIKE_WINDOW = timedelta(minutes=10)


class TrapVerdict(StrEnum):
    SKIP = "skip"
    BYPASS = "bypass"
    BAN = "ban"


@dataclass(frozen=True, slots=True)
class FunnelCounts:
    joined: int
    verified: int
    trapped: int
    left_within_24h: int


def trap_verdict(
    *,
    is_bot: bool,
    is_webhook: bool,
    rank: Rank | str,
    is_administrator: bool,
    is_guild_owner: bool,
) -> TrapVerdict:
    if is_bot or is_webhook:
        return TrapVerdict.SKIP
    resolved = rank if isinstance(rank, Rank) else _rank(rank)
    if resolved in STAFF_RANKS:
        return TrapVerdict.SKIP
    if bypasses_limits(is_administrator=is_administrator, is_guild_owner=is_guild_owner):
        return TrapVerdict.BYPASS
    return TrapVerdict.BAN


def everyone_can_send(*, overwrite: bool | None, inherit: bool) -> bool:
    if overwrite is False:
        return False
    if overwrite is True:
        return True
    return bool(inherit)


def join_spike_due(
    *,
    joined: int,
    floor: int,
    last: datetime | None,
    now: datetime,
) -> bool:
    if int(joined) < int(floor):
        return False
    return should_emit(last, now=as_utc(now), interval=JOIN_SPIKE_INTERVAL)


def _rank(token: str) -> Rank:
    key = str(token).strip().lower()
    for item in Rank:
        if item.key == key:
            return item
    return Rank.NONE
