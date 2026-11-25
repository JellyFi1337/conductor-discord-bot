"""Склейка частых правок одной карточки в одно окно."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from typing import Any

__all__ = ["CardRefresher"]

Render = Callable[[], Awaitable[None]]


class CardRefresher:
    """Первый запрос ставит окно; следующие только подменяют render."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._renders: dict[Hashable, Render] = {}

    def request(self, key: tuple, render: Render, *, window: float = 2.0) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        armed = key in self._renders
        self._renders[key] = render
        if armed:
            return
        schedule(("card", key), float(window), lambda: self._flush(key))

    def cancel(self, key: tuple) -> None:
        self._renders.pop(key, None)
        queue = getattr(self._bot, "queue", None)
        drop = getattr(queue, "cancel_later", None)
        if callable(drop):
            drop(("card", key))

    async def _flush(self, key: Hashable) -> None:
        render = self._renders.pop(key, None)
        if render is None:
            return
        await render()
