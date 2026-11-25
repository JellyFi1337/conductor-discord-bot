"""Периодические задачи с джиттером. Запуск — через bot.spawn.

Без джиттера все сверки бьют в одну секунду минуты. Здесь период размазывается
на ±15%, а первый запуск сдвигается по имени задачи.
"""

from __future__ import annotations

import asyncio
import random
import zlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from conductor.domain.traffic import Lane, current_lane

log = structlog.get_logger("conductor.scheduler")

JITTER_RATIO = 0.15
MINUTE_SECONDS = 60.0

__all__ = ["JITTER_RATIO", "Scheduler", "jittered_period", "start_offset"]


class Spawner(Protocol):
    def __call__(self, coro: Coroutine[Any, Any, Any], *, name: str) -> Any: ...


type JobFn = Callable[[], Coroutine[Any, Any, Any]]


def jittered_period(period: float, rng: random.Random) -> float:
    """Следующий интервал: период ±15%. Никогда не отрицательный."""
    if period <= 0:
        return 0.0
    factor = 1.0 + rng.uniform(-JITTER_RATIO, JITTER_RATIO)
    return max(0.0, period * factor)


def start_offset(name: str, rng: random.Random) -> float:
    """Первый запуск внутри минуты, по имени — чтобы задачи не совпали на :00."""
    slot = zlib.crc32(name.encode("utf-8")) % int(MINUTE_SECONDS)
    return max(0.0, slot + rng.uniform(-0.5, 0.5))


@dataclass(slots=True)
class _Job:
    name: str
    period: float
    fn: JobFn


class Scheduler:
    """Реестр периодических задач из cog'ов. Сами фичи здесь не регистрируются."""

    def __init__(
        self,
        spawn: Spawner,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._spawn = spawn
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._jobs: list[_Job] = []
        self._running = False

    def every(self, period: float, name: str, fn: JobFn) -> None:
        job = _Job(name=name, period=period, fn=fn)
        self._jobs.append(job)
        if self._running:
            self._spawn_loop(job)

    @property
    def job_names(self) -> tuple[str, ...]:
        return tuple(job.name for job in self._jobs)

    @property
    def job_count(self) -> int:
        return len(self._jobs)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for job in self._jobs:
            self._spawn_loop(job)

    def stop(self) -> None:
        self._running = False

    def _spawn_loop(self, job: _Job) -> None:
        self._spawn(self._loop(job), name=f"sched-{job.name}")

    async def _loop(self, job: _Job) -> None:
        await self._pause(start_offset(job.name, self._rng))
        while self._running:
            log.debug("scheduled_job_start", job=job.name)
            token = current_lane.set(Lane.BACKGROUND)
            try:
                task = self._spawn(job.fn(), name=job.name)
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # The spawner's done-callback already logs the traceback once; here only the fact.
                    log.warning("scheduled_job_failed", job=job.name, error=repr(error))
            finally:
                current_lane.reset(token)
            await self._pause(jittered_period(job.period, self._rng))

    async def _pause(self, delay: float) -> None:
        if delay <= 0:
            return
        if self._sleep is not None:
            await self._sleep(delay)
            return
        await asyncio.sleep(delay)
