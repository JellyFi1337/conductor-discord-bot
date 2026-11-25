"""Исход действия, после которого UI ещё должен ответить, и отложенное разрушение."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Teardown = Callable[[], Awaitable[None]]

__all__ = ["Outcome", "Teardown"]


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    error: str | None = None
    detail: str | None = None
