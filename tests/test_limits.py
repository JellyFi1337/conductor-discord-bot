from __future__ import annotations

import asyncio

import pytest

from conductor.presentation.copy import WAIT_BODY, WAIT_TITLE
from conductor.presentation.layout import collected_text
from conductor.services.limits import InteractionLimiter, OutboundGate


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


@pytest.mark.asyncio
async def test_outbound_semaphore_caps_concurrent_work() -> None:
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


def test_interaction_limiter_window_denies_then_recovers() -> None:
    clock = FakeClock()
    limiter = InteractionLimiter(limit=2, window=60, clock=clock)
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
    assert limiter.trips == 1
    text = collected_text(limiter.deny_view())
    assert WAIT_TITLE in text
    assert WAIT_BODY in text
    clock.advance(60)
    assert limiter.allow(1) is True
    assert limiter.allow(2) is True


def test_three_hundred_members_each_click_once() -> None:
    clock = FakeClock()
    limiter = InteractionLimiter(limit=1200, window=60, member_limit=12, member_window=10, clock=clock)
    assert all(limiter.allow(1, user) for user in range(300))
    assert limiter.trips == 0


def test_one_member_burst_is_capped() -> None:
    clock = FakeClock()
    limiter = InteractionLimiter(limit=1200, window=60, member_limit=12, member_window=10, clock=clock)
    accepted = sum(1 for _ in range(30) if limiter.allow(1, 7))
    assert accepted == 12
    assert limiter.trips == 18
    clock.advance(10)
    assert limiter.allow(1, 7) is True
