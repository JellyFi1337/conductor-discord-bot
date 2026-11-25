"""Блокировка create/delete ролей, каналов и эмодзи. Без Discord и без IO."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

BUCKET_ROLE = "role"
BUCKET_CHANNEL = "channel"
BUCKET_EMOJI = "emoji"
BUCKET_DELETE = "delete"
BUCKETS = (BUCKET_ROLE, BUCKET_CHANNEL, BUCKET_EMOJI, BUCKET_DELETE)

PERSIST_CAP = 3600.0
OBSERVE_MATCH_WINDOW = 10.0
CLOUDFLARE_FALLBACK = 60.0


class HoldSource(StrEnum):
    DISCORD = "discord"
    DISCORD_SHARED = "shared"
    LIBRARY = "library"
    LOCAL = "local"


PERSIST_SOURCES = frozenset({HoldSource.DISCORD})


@dataclass(frozen=True, slots=True)
class CreateHold:
    bucket: str
    until: float
    retry_after: float
    source: HoldSource = HoldSource.LIBRARY
    route: str = ""

    def remaining(self, now: float) -> float:
        return max(0.0, float(self.until) - float(now))

    def active(self, now: float) -> bool:
        return self.remaining(now) > 0.0


@dataclass(frozen=True, slots=True)
class ObservedLimit:
    guild_id: int
    bucket: str
    status: int
    scope: str
    retry_after: float | None
    remaining: int | None
    reset_after: float | None
    at: float


def classify(
    *,
    rate_limited: bool,
    status: int | None,
    observed: ObservedLimit | None,
    now: float,
) -> HoldSource | None:
    if observed is not None and int(observed.status) == 429:
        age = float(now) - float(observed.at)
        if 0.0 <= age <= OBSERVE_MATCH_WINDOW:
            if observed.scope == "shared":
                return HoldSource.DISCORD_SHARED
            return HoldSource.DISCORD
    if rate_limited:
        return HoldSource.LIBRARY
    if status == 429:
        return HoldSource.DISCORD
    return None


def persisted_until(hold: CreateHold, now: float) -> float | None:
    if hold.source not in PERSIST_SOURCES:
        return None
    return min(float(hold.until), float(now) + PERSIST_CAP)


def hold_from_retry(
    *,
    bucket: str,
    retry_after: float,
    now: float,
    source: HoldSource = HoldSource.DISCORD,
    route: str = "",
) -> CreateHold:
    delay = max(0.0, float(retry_after))
    return CreateHold(
        bucket=str(bucket),
        until=float(now) + delay,
        retry_after=delay,
        source=source,
        route=str(route),
    )


def hold_from_wait(
    *,
    bucket: str,
    wait: float,
    now: float,
    source: HoldSource = HoldSource.LOCAL,
    route: str = "",
) -> CreateHold:
    delay = max(0.0, float(wait))
    return CreateHold(
        bucket=str(bucket),
        until=float(now) + delay,
        retry_after=delay,
        source=source,
        route=str(route),
    )


__all__ = [
    "BUCKETS",
    "BUCKET_CHANNEL",
    "BUCKET_DELETE",
    "BUCKET_EMOJI",
    "BUCKET_ROLE",
    "CLOUDFLARE_FALLBACK",
    "CreateHold",
    "HoldSource",
    "OBSERVE_MATCH_WINDOW",
    "ObservedLimit",
    "PERSIST_CAP",
    "PERSIST_SOURCES",
    "classify",
    "hold_from_retry",
    "hold_from_wait",
    "persisted_until",
]
