"""Очередь по ключу, дребезг и отложенные действия.

Повторная постановка того же ключа не плодит работу, а обновляет уже стоящую.
Отложенное действие живёт в расписании due-времени и отменяется по ключу —
не отдельной спящей корутиной на каждый вызов.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any

import structlog

from conductor.services.scheduler import Spawner

log = structlog.get_logger("conductor.queue")

__all__ = ["ActionQueue", "UniqueQueue"]


class UniqueQueue[T]:
    """Одна работа на ключ: повторный put заменяет полезную нагрузку."""

    def __init__(self) -> None:
        self._items: OrderedDict[Hashable, T] = OrderedDict()

    def put(self, key: Hashable, payload: T) -> None:
        self._items[key] = payload

    def pop(self) -> tuple[Hashable, T] | None:
        if not self._items:
            return None
        return self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items


@dataclass(slots=True)
class _Later:
    due: float
    seq: int
    key: Hashable = field(compare=False)
    factory: Callable[[], Awaitable[Any]] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def __lt__(self, other: _Later) -> bool:
        return (self.due, self.seq) < (other.due, other.seq)


class ActionQueue:
    """Дребезг, очередь по ключу и отложенные действия с отменой."""

    def __init__(
        self,
        spawn: Spawner,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.jobs: UniqueQueue[Any] = UniqueQueue()
        self._spawn = spawn
        self._clock = clock or time.monotonic
        self._ready_at: dict[Hashable, float] = {}
        self._pending: dict[Hashable, _Later] = {}
        self._heap: list[_Later] = []
        self._serial: dict[Hashable, list[Callable[[], Awaitable[Any]]]] = {}
        self._serial_busy: set[Hashable] = set()
        self._seq = 0
        self._running = False
        self._wakeup = asyncio.Event()

    def submit(self, key: Hashable, job: Callable[[], Awaitable[Any]]) -> None:
        """Поставить работу в хвост ключа. Один ключ обрабатывается строго по очереди."""
        bucket = self._serial.setdefault(key, [])
        bucket.append(job)
        if key in self._serial_busy:
            return
        self._serial_busy.add(key)
        self._spawn(self._drain_serial(key), name=f"queue-{key}")

    def debounce(self, key: Hashable, interval: float) -> bool:
        """True, если вызов проходит. Второй в пределах interval — нет."""
        now = self._clock()
        self._prune_ready(now)
        until = self._ready_at.get(key, 0.0)
        if now < until:
            return False
        self._ready_at[key] = now + max(0.0, interval)
        return True

    def until(self, key: Hashable) -> float:
        """Сколько секунд осталось до снятия окна. 0 — можно сразу."""
        now = self._clock()
        ready = self._ready_at.get(key, 0.0)
        remaining = ready - now
        if remaining <= 0:
            self._ready_at.pop(key, None)
            return 0.0
        return remaining

    def touch(self, key: Hashable, interval: float) -> None:
        """Отметить, что ключ только что сработал, и открыть новое окно."""
        now = self._clock()
        self._prune_ready(now)
        self._ready_at[key] = now + max(0.0, interval)

    def schedule_later(self, key: Hashable, delay: float, job: Callable[[], Awaitable[Any]]) -> None:
        self.cancel_later(key)
        self._seq += 1
        item = _Later(due=self._clock() + max(0.0, delay), seq=self._seq, key=key, factory=job)
        self._pending[key] = item
        heapq.heappush(self._heap, item)
        self._wakeup.set()

    def cancel_later(self, key: Hashable) -> bool:
        item = self._pending.pop(key, None)
        if item is None:
            return False
        item.cancelled = True
        return True

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._spawn(self._pump(), name="queue-later")

    @property
    def job_count(self) -> int:
        return len(self.jobs) + sum(len(bucket) for bucket in self._serial.values())

    @property
    def later_count(self) -> int:
        return sum(1 for item in self._pending.values() if not item.cancelled)

    def stop(self) -> None:
        self._running = False
        self._wakeup.set()

    async def fire_due(self) -> int:
        """Запустить все действия, чей срок наступил. Для тестов на поддельных часах."""
        now = self._clock()
        fired = 0
        while self._heap and self._heap[0].due <= now:
            item = heapq.heappop(self._heap)
            if item.cancelled or self._pending.get(item.key) is not item:
                continue
            self._pending.pop(item.key, None)
            fired += 1
            self._spawn(self._run(item), name=f"later-{item.key}")
        return fired

    async def _drain_serial(self, key: Hashable) -> None:
        try:
            while True:
                bucket = self._serial.get(key)
                if not bucket:
                    return
                factory = bucket.pop(0)
                try:
                    await factory()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("queued_action_failed", key=str(key))
        finally:
            self._serial_busy.discard(key)
            leftover = self._serial.get(key)
            if leftover:
                self._serial_busy.add(key)
                self._spawn(self._drain_serial(key), name=f"queue-{key}")
            elif key in self._serial:
                self._serial.pop(key, None)

    async def _run(self, item: _Later) -> None:
        try:
            await item.factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("deferred_action_failed", key=str(item.key))

    async def _pump(self) -> None:
        while self._running:
            delay = self._delay_until_next()
            if delay is None:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await self._wait(delay)
            if self._running:
                await self.fire_due()

    def _delay_until_next(self) -> float | None:
        while self._heap and (self._heap[0].cancelled or self._pending.get(self._heap[0].key) is not self._heap[0]):
            heapq.heappop(self._heap)
        if not self._heap:
            return None
        return max(0.0, self._heap[0].due - self._clock())

    def _prune_ready(self, now: float | None = None) -> None:
        moment = self._clock() if now is None else now
        stale = [key for key, until in self._ready_at.items() if until <= moment]
        for key in stale:
            del self._ready_at[key]

    async def _wait(self, delay: float) -> None:
        if delay <= 0:
            return
        await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
