from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from conductor.domain.records import CachedMessage
from conductor.presentation.copy import WAIT_BODY, WAIT_TITLE
from conductor.presentation.layout import collected_text
from conductor.services.cache import (
    GUILD_CONFIG_MAXSIZE,
    PERMISSIONS_MAXSIZE,
    RANKS_MAXSIZE,
    CacheHub,
    TTLCache,
    roles_changed,
)
from conductor.services.limits import InteractionLimiter, OutboundGate
from conductor.services.message_cache import MessageCache


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _message(message_id: int, guild_id: int, content: str = "текст") -> CachedMessage:
    return CachedMessage(
        id=message_id,
        guild_id=guild_id,
        channel_id=10,
        author_id=20,
        author_name="Ann",
        content=content,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        attachments=(),
    )


def test_ttl_expires_and_counts_as_miss() -> None:
    clock = FakeClock()
    cache = TTLCache[str, int](ttl=10, clock=clock, name="t")
    cache.set("rank", 3)
    assert cache.get("rank") == 3
    assert cache.hits == 1
    clock.advance(10)
    assert cache.get("rank") is None
    assert cache.misses == 1
    assert len(cache) == 0


def test_invalidate_drops_key_immediately() -> None:
    cache = TTLCache[tuple[int, int], str](ttl=60)
    cache.set((1, 2), "senior")
    assert cache.invalidate((1, 2)) is True
    assert cache.get((1, 2)) is None
    assert cache.invalidations == 1


def test_maxsize_evicts_oldest() -> None:
    cache = TTLCache[str, int](ttl=None, maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert cache.stats.maxsize == 2
    assert cache.stats.size == 2


def test_get_refreshes_lru() -> None:
    cache = TTLCache[str, int](ttl=None, maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_hub_limits_match_the_plan() -> None:
    hub = CacheHub()
    assert hub.guild_config.maxsize == GUILD_CONFIG_MAXSIZE == 1_000
    assert hub.ranks.maxsize == RANKS_MAXSIZE == 50_000
    assert hub.permissions.maxsize == PERMISSIONS_MAXSIZE == 1_000
    usage = {name: (size, cap) for name, size, cap in hub.occupancy()}
    assert usage["guild_config"] == (0, 1_000)
    assert usage["member_joined"][1] == RANKS_MAXSIZE
    assert usage["suspensions"][0] == 0


def test_role_change_clears_member_rank() -> None:
    hub = CacheHub()
    hub.ranks.set((7, 9), "chief")
    hub.permissions.set((7, "mod.ban"), True)
    hub.ranks.set((8, 9), "watcher")
    assert roles_changed((1, 2), (1, 3))
    hub.invalidate_member(7, 9)
    assert hub.ranks.get((7, 9)) is None
    hub.ranks.set((7, 11), "senior")
    hub.invalidate_access(7)
    assert hub.ranks.get((7, 11)) is None
    assert hub.permissions.get((7, "mod.ban")) is None
    assert hub.ranks.get((8, 9)) == "watcher"


def test_joined_and_suspensions_invalidate_separately() -> None:
    hub = CacheHub()
    hub.member_joined.set((7, 9), None)
    hub.suspensions.set(7, {9: True})
    hub.invalidate_member(7, 9)
    assert hub.member_joined.has((7, 9))
    hub.invalidate_joined(7, 9)
    assert not hub.member_joined.has((7, 9))
    assert hub.suspensions.has(7)
    hub.invalidate_suspensions(7)
    assert not hub.suspensions.has(7)


def test_message_cache_ttl_and_guild_cap() -> None:
    clock = FakeClock()
    cache = MessageCache(maxlen=3, ttl=5, clock=clock)
    cache.store(_message(1, guild_id=1))
    cache.store(_message(2, guild_id=1))
    cache.store(_message(3, guild_id=2))
    cache.store(_message(4, guild_id=1))
    assert len(cache) == 3
    assert cache.get(3) is not None
    assert cache.size_for(1) <= 2
    clock.advance(5)
    assert cache.get(3) is None
    assert cache.get(4) is None


def test_drop_author_leaves_other_people() -> None:
    cache = MessageCache(maxlen=10, ttl=60)
    cache.store(_message(1, guild_id=1))
    other = CachedMessage(
        id=2,
        guild_id=1,
        channel_id=10,
        author_id=99,
        author_name="Bob",
        content="другое",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        attachments=(),
    )
    cache.store(other)
    assert cache.drop_author(1, 20) == 1
    assert cache.get(1) is None
    assert cache.get(2) is not None


def test_busy_guild_does_not_wipe_the_other() -> None:
    cache = MessageCache(maxlen=3, ttl=60)
    cache.store(_message(1, guild_id=1))
    cache.store(_message(2, guild_id=1))
    cache.store(_message(3, guild_id=2))
    cache.store(_message(4, guild_id=1))
    assert cache.get(3) is not None
    assert cache.pop(3) is not None
    assert cache.get(3) is None


@pytest.mark.asyncio
async def test_outbound_semaphore_does_not_exceed_limit() -> None:
    gate = OutboundGate(limit=2)
    running = 0
    peak = 0

    async def worker() -> None:
        nonlocal running, peak
        async with gate:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    assert peak == 2
    assert gate.depth == 0


def test_interaction_limiter_denies_then_recovers() -> None:
    clock = FakeClock()
    limiter = InteractionLimiter(limit=2, window=60, clock=clock)
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
    assert limiter.trips == 1
    text = collected_text(limiter.deny_view())
    assert WAIT_TITLE in text
    assert WAIT_BODY in text
    assert "подождите" in WAIT_TITLE.lower() or "подождите" in WAIT_BODY.lower()
    clock.advance(60)
    assert limiter.allow(1) is True
