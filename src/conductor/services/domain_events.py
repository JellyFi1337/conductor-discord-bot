"""Шина доменных событий. Подписчики секций не ходят друг в друга напрямую."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Handler = Callable[[Any], None]


class DomainEvents:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[Handler]] = {}

    def subscribe(self, event_type: type[Any], handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def emit(self, event: object) -> None:
        for handler in list(self._handlers.get(type(event), ())):
            handler(event)
