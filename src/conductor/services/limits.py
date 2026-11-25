"""Семафор исходящих изменений Discord и лимитер нажатий.

Страж из 1g только вызывает лимитер; собственной реализации ограничения у него нет.
Массовые операции берут исходящий семафор. Нажатия — окно на участника плюс потолок гильдии.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from discord import ui

from conductor.presentation.layout import wait as wait_card

OUTBOUND_LIMIT = 8
INTERACTION_LIMIT = 50
INTERACTION_WINDOW = 60.0
MEMBER_CLICK_LIMIT = 12
MEMBER_CLICK_WINDOW = 10.0

__all__ = [
    "INTERACTION_LIMIT",
    "INTERACTION_WINDOW",
    "MEMBER_CLICK_LIMIT",
    "MEMBER_CLICK_WINDOW",
    "InteractionLimiter",
    "OUTBOUND_LIMIT",
    "OutboundGate",
    "wait_card",
]


class OutboundGate:
    """Единая точка для массовых исходящих изменений Discord."""

    def __init__(self, limit: int = OUTBOUND_LIMIT, *, governor: Any | None = None) -> None:
        self._governor = governor
        if governor is None:
            self.limit = max(1, limit)
            self._sem = asyncio.Semaphore(self.limit)
        else:
            self.limit = int(getattr(getattr(governor, "limits", None), "outbound_concurrency", limit) or limit)
            self._sem = None
        self._in_use = 0
        self._waiting = 0

    @property
    def depth(self) -> int:
        return self._in_use

    @property
    def waiting(self) -> int:
        return self._waiting

    async def __aenter__(self) -> OutboundGate:
        self._waiting += 1
        try:
            if self._governor is not None:
                await self._governor.acquire_lane()
            else:
                await self._sem.acquire()
        finally:
            self._waiting -= 1
        self._in_use += 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._in_use -= 1
        if self._governor is not None:
            self._governor.release_lane()
            return
        self._sem.release()


class InteractionLimiter:
    """Окно на участника и потолок гильдии (`INTERACTION_LIMIT_PER_MINUTE`).

    Без `user_id` остаётся только гильдейское окно — так работают старые вызовы.
    Мутации Discord из нажатия идут через регулятор, этот лимитер их не режет.
    """

    def __init__(
        self,
        *,
        limit: int = INTERACTION_LIMIT,
        window: float = INTERACTION_WINDOW,
        member_limit: int = MEMBER_CLICK_LIMIT,
        member_window: float = MEMBER_CLICK_WINDOW,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limit = max(1, limit)
        self.window = window
        self.member_limit = max(1, int(member_limit))
        self.member_window = float(member_window)
        self._clock = clock or time.monotonic
        self._guild: dict[int, deque[float]] = defaultdict(deque)
        self._member: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.trips = 0

    def allow(self, guild_id: int, user_id: int | None = None) -> bool:
        now = self._clock()
        gid = int(guild_id)
        if user_id is not None:
            member_key = (gid, int(user_id))
            member_hits = self._member[member_key]
            _prune(member_hits, now, self.member_window)
            if len(member_hits) >= self.member_limit:
                self.trips += 1
                return False
        guild_hits = self._guild[gid]
        _prune(guild_hits, now, self.window)
        if len(guild_hits) >= self.limit:
            self.trips += 1
            return False
        guild_hits.append(now)
        if user_id is not None:
            self._member[(gid, int(user_id))].append(now)
        return True

    def deny_view(self) -> ui.LayoutView:
        return wait_card()


def _prune(bucket: deque[float], now: float, window: float) -> None:
    cutoff = now - window
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
