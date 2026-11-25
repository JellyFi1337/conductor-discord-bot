from __future__ import annotations

import asyncio
from types import SimpleNamespace

from conductor.services.card_refresh import CardRefresher
from conductor.services.queue import ActionQueue


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += float(delta)


def _spawn_box() -> tuple[list[asyncio.Task[object]], object]:
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, name: str | None = None) -> None:
        del name
        spawned.append(asyncio.create_task(coro))  # type: ignore[arg-type]

    return spawned, spawn


async def test_card_refresh_coalesces_requests_in_one_window() -> None:
    clock = FakeClock()
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=clock)
    refresher = CardRefresher(SimpleNamespace(queue=queue))
    calls: list[int] = []

    async def render() -> None:
        calls.append(1)

    for _ in range(50):
        refresher.request(("g", 1), render, window=2.0)
    clock.advance(1.9)
    assert await queue.fire_due() == 0
    clock.advance(0.1)
    assert await queue.fire_due() == 1
    if spawned:
        await asyncio.gather(*spawned)
        spawned.clear()
    assert calls == [1]
    refresher.request(("g", 1), render, window=2.0)
    clock.advance(2.0)
    assert await queue.fire_due() == 1
    if spawned:
        await asyncio.gather(*spawned)
    assert calls == [1, 1]
