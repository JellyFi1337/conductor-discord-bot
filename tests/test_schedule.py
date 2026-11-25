from __future__ import annotations

import asyncio
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from conductor.services.heartbeat import PERIOD_SECONDS, STALE_SECONDS, heartbeat_path, touch
from conductor.services.queue import ActionQueue, UniqueQueue
from conductor.services.scheduler import JITTER_RATIO, jittered_period, start_offset


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def test_jitter_stays_inside_declared_band() -> None:
    rng = random.Random(0)
    period = 100.0
    samples = [jittered_period(period, rng) for _ in range(500)]
    low = period * (1.0 - JITTER_RATIO)
    high = period * (1.0 + JITTER_RATIO)
    assert all(low <= sample <= high for sample in samples)
    assert min(samples) < period * 0.92
    assert max(samples) > period * 1.08


def test_heartbeat_touches_data_dir_and_survives_a_bad_path(tmp_path: Path) -> None:
    settings = SimpleNamespace(data_dir=tmp_path / "data")
    path = heartbeat_path(settings)
    assert path.parent == tmp_path / "data"
    assert touch(path) is True
    assert path.is_file()
    first = path.stat().st_mtime_ns
    assert touch(path) is True
    assert path.stat().st_mtime_ns >= first
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    assert touch(blocker / "nested" / ".heartbeat") is False


def test_heartbeat_stale_window_covers_several_periods() -> None:
    # healthcheck в docker-compose.yml сверяется с этим же числом.
    assert STALE_SECONDS >= PERIOD_SECONDS * 3
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert f"< {STALE_SECONDS}" in compose


def test_start_offsets_spread_across_the_minute() -> None:
    rng = random.Random(1)
    offsets = [start_offset(f"job-{index}", rng) for index in range(30)]
    assert all(-0.5 <= offset <= 60.5 for offset in offsets)
    assert len({round(offset) for offset in offsets}) > 5


def test_duplicate_keys_update_the_same_job() -> None:
    queue: UniqueQueue[str] = UniqueQueue()
    queue.put("user:1", "first")
    queue.put("user:2", "other")
    queue.put("user:1", "updated")
    assert len(queue) == 2
    assert queue.pop() == ("user:1", "updated")
    assert queue.pop() == ("user:2", "other")
    assert queue.pop() is None


def test_debounce_rejects_the_second_call() -> None:
    clock = FakeClock()
    board = ActionQueue(asyncio.create_task, clock=clock)
    assert board.debounce("room:ann", 2.0) is True
    assert board.debounce("room:ann", 2.0) is False
    clock.advance(1.9)
    assert board.debounce("room:ann", 2.0) is False
    clock.advance(0.1)
    assert board.debounce("room:ann", 2.0) is True
    assert board.debounce("room:bob", 2.0) is True
    assert board.until("room:ann") == 2.0
    board.touch("room:ann", 5.0)
    assert board.until("room:ann") == 5.0
    clock.advance(5.0)
    assert board.until("room:ann") == 0.0


def test_expired_debounce_windows_are_pruned() -> None:
    clock = FakeClock()
    board = ActionQueue(asyncio.create_task, clock=clock)
    for index in range(20):
        assert board.debounce(f"k{index}", 1.0) is True
    assert len(board._ready_at) == 20
    clock.advance(1.0)
    assert board.debounce("fresh", 1.0) is True
    assert "k0" not in board._ready_at
    assert list(board._ready_at) == ["fresh"]


@pytest.mark.asyncio
async def test_deferred_action_cancels_and_does_not_fire_twice() -> None:
    clock = FakeClock()
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    board = ActionQueue(spawn, clock=clock)
    fired: list[str] = []

    async def boom() -> None:
        fired.append("boom")

    async def once() -> None:
        fired.append("once")

    board.schedule_later("room:1", 40, boom)
    board.schedule_later("room:1", 40, once)
    clock.advance(40)
    assert await board.fire_due() == 1
    if spawned:
        await asyncio.gather(*spawned)
    spawned.clear()
    assert fired == ["once"]

    async def gone() -> None:
        fired.append("gone")

    board.schedule_later("room:2", 10, gone)
    assert board.cancel_later("room:2") is True
    clock.advance(10)
    assert await board.fire_due() == 0
    if spawned:
        await asyncio.gather(*spawned)
    assert "gone" not in fired


@pytest.mark.asyncio
async def test_submit_runs_the_same_key_in_order() -> None:
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    board = ActionQueue(spawn)
    order: list[int] = []
    gate = asyncio.Event()

    async def first() -> None:
        await gate.wait()
        order.append(1)

    async def second() -> None:
        order.append(2)

    board.submit("user:1", first)
    board.submit("user:1", second)
    assert board.job_count == 2
    await asyncio.sleep(0)
    assert order == []
    gate.set()
    if spawned:
        await asyncio.gather(*spawned)
    assert order == [1, 2]
    assert board.job_count == 0
