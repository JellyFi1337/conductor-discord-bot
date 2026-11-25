"""TTL-кэши с явной инвалидацией и метрикой попаданий.

Память здесь — только кэш. Источник правды остаётся в БД. Где есть запись,
там должно быть событие сброса: иначе кэш врёт после смены роли или конфига.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

RANK_TTL = 45.0
PERMISSION_TTL = 60.0
ROOM_OWNER_TTL = 60.0
PULSE_TTL = 30.0
LIMIT_BYPASS_TTL = 86400.0
AUTOMOD_WINDOW_TTL = 3600.0
MEMBER_JOINED_TTL = 3600.0
SUSPENSIONS_TTL = 60.0
SHOWCASE_TTL = 60.0
GUILD_CONFIG_MAXSIZE = 1_000
RANKS_MAXSIZE = 50_000
PERMISSIONS_MAXSIZE = 1_000
DEFAULT_MAXSIZE = 10_000

__all__ = [
    "AUTOMOD_WINDOW_TTL",
    "CacheHub",
    "CacheMapping",
    "CacheStats",
    "DEFAULT_MAXSIZE",
    "GUILD_CONFIG_MAXSIZE",
    "LIMIT_BYPASS_TTL",
    "MEMBER_JOINED_TTL",
    "PERMISSIONS_MAXSIZE",
    "PERMISSION_TTL",
    "PULSE_TTL",
    "RANKS_MAXSIZE",
    "RANK_TTL",
    "ROOM_OWNER_TTL",
    "SHOWCASE_TTL",
    "SUSPENSIONS_TTL",
    "TTLCache",
    "roles_changed",
]


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    size: int
    invalidations: int
    maxsize: int | None = None


@dataclass(slots=True)
class _Entry[V]:
    value: V
    expires_at: float | None


class TTLCache[K, V]:
    """Кэш с TTL и ручным сбросом. Промах по сроку — это промах, не попадание."""

    def __init__(
        self,
        *,
        ttl: float | None,
        clock: Callable[[], float] | None = None,
        name: str = "",
        maxsize: int | None = None,
    ) -> None:
        self.name = name
        self.maxsize = maxsize
        self._ttl = ttl
        self._clock = clock or time.monotonic
        self._data: OrderedDict[K, _Entry[V]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    def get(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at is not None and entry.expires_at <= self._clock():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return entry.value

    def has(self, key: K) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return False
        if entry.expires_at is not None and entry.expires_at <= self._clock():
            self._data.pop(key, None)
            return False
        return True

    def set(self, key: K, value: V) -> None:
        expires_at = None if self._ttl is None else self._clock() + self._ttl
        self._data[key] = _Entry(value=value, expires_at=expires_at)
        self._data.move_to_end(key)
        self._trim()

    def invalidate(self, key: K) -> bool:
        removed = self._data.pop(key, None) is not None
        if removed:
            self.invalidations += 1
        return removed

    def invalidate_prefix(self, prefix: tuple[object, ...]) -> int:
        if not prefix:
            return 0
        drop = [key for key in self._data if _matches_prefix(key, prefix)]
        for key in drop:
            del self._data[key]
        self.invalidations += len(drop)
        return len(drop)

    def clear(self) -> None:
        n = len(self._data)
        self._data.clear()
        self.invalidations += n

    def __len__(self) -> int:
        self._drop_expired()
        return len(self._data)

    def items(self) -> list[tuple[K, V]]:
        """Snapshot of live entries; safe to iterate while the cache changes."""
        self._drop_expired()
        return [(key, entry.value) for key, entry in self._data.items()]

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self.hits,
            misses=self.misses,
            size=len(self),
            invalidations=self.invalidations,
            maxsize=self.maxsize,
        )

    def _trim(self) -> None:
        if self.maxsize is None:
            return
        # Полный проход по просроченным — O(n) на каждый set; делаем его только когда переполнены.
        if len(self._data) <= self.maxsize:
            return
        self._drop_expired()
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def _drop_expired(self) -> None:
        now = self._clock()
        stale = [key for key, entry in self._data.items() if entry.expires_at is not None and entry.expires_at <= now]
        for key in stale:
            del self._data[key]


class CacheMapping[K, V]:
    """Словарь поверх TTLCache: .get и запись, без собственного срока жизни."""

    def __init__(self, cache: TTLCache[K, V]) -> None:
        self._cache = cache

    def get(self, key: K, default: V | None = None) -> V | None:
        value = self._cache.get(key)
        return default if value is None else value

    def __setitem__(self, key: K, value: V) -> None:
        self._cache.set(key, value)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, Hashable):
            return False
        return self._cache.has(key)  # type: ignore[arg-type]

    def __iter__(self) -> Iterator[K]:
        return iter([key for key, _ in self._cache.items()])

    def __len__(self) -> int:
        return len(self._cache)

    def items(self) -> list[tuple[K, V]]:
        return self._cache.items()

    def values(self) -> list[V]:
        return [value for _, value in self._cache.items()]


class CacheHub:
    """Именные кэши, на которых держатся конфиг, ранги, ключи прав и владелец комнаты."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        tick = clock or time.monotonic
        self._clock = tick
        self.room_owner: TTLCache[tuple[int, int], int] = TTLCache(
            ttl=ROOM_OWNER_TTL,
            clock=tick,
            name="room_owner",
            maxsize=DEFAULT_MAXSIZE,
        )
        self.guild_config: TTLCache[int, Any] = TTLCache(
            ttl=None,
            clock=tick,
            name="guild_config",
            maxsize=GUILD_CONFIG_MAXSIZE,
        )
        self.ranks: TTLCache[tuple[int, int], Any] = TTLCache(
            ttl=RANK_TTL,
            clock=tick,
            name="ranks",
            maxsize=RANKS_MAXSIZE,
        )
        self.permissions: TTLCache[tuple[int, str], Any] = TTLCache(
            ttl=PERMISSION_TTL,
            clock=tick,
            name="permissions",
            maxsize=PERMISSIONS_MAXSIZE,
        )
        self.pulse: TTLCache[int, Any] = TTLCache(
            ttl=PULSE_TTL,
            clock=tick,
            name="pulse",
            maxsize=DEFAULT_MAXSIZE,
        )
        self.limit_bypass: TTLCache[tuple[int, int, str], bool] = TTLCache(
            ttl=LIMIT_BYPASS_TTL,
            clock=tick,
            name="limit_bypass",
            maxsize=DEFAULT_MAXSIZE,
        )
        self.automod_windows: TTLCache[tuple[int, int], Any] = TTLCache(
            ttl=AUTOMOD_WINDOW_TTL,
            clock=tick,
            name="automod_windows",
            maxsize=DEFAULT_MAXSIZE,
        )
        self.member_joined: TTLCache[tuple[int, int], datetime | None] = TTLCache(
            ttl=MEMBER_JOINED_TTL,
            clock=tick,
            name="member_joined",
            maxsize=RANKS_MAXSIZE,
        )
        self.suspensions: TTLCache[int, dict[int, Any]] = TTLCache(
            ttl=SUSPENSIONS_TTL,
            clock=tick,
            name="suspensions",
            maxsize=PERMISSIONS_MAXSIZE,
        )
        self.showcase: TTLCache[int, dict[str, str]] = TTLCache(
            ttl=SHOWCASE_TTL,
            clock=tick,
            name="showcase",
            maxsize=PERMISSIONS_MAXSIZE,
        )

    def set_automod_window_ttl(self, seconds: float) -> None:
        self.automod_windows = TTLCache(
            ttl=max(1.0, float(seconds)),
            clock=self._clock,
            name="automod_windows",
            maxsize=DEFAULT_MAXSIZE,
        )

    def occupancy(self) -> tuple[tuple[str, int, int], ...]:
        return tuple((cache.name, len(cache), int(cache.maxsize or 0)) for cache in self._all())

    def invalidate_member(self, guild_id: int, user_id: int) -> None:
        self.ranks.invalidate((guild_id, user_id))

    def invalidate_joined(self, guild_id: int, user_id: int) -> None:
        self.member_joined.invalidate((guild_id, user_id))

    def invalidate_suspensions(self, guild_id: int) -> None:
        self.suspensions.invalidate(int(guild_id))

    def invalidate_access(self, guild_id: int) -> None:
        self.ranks.invalidate_prefix((guild_id,))
        self.permissions.invalidate_prefix((guild_id,))

    def invalidate_room(self, guild_id: int, channel_id: int) -> None:
        self.room_owner.invalidate((guild_id, channel_id))

    def invalidate_guild_rooms(self, guild_id: int) -> None:
        self.room_owner.invalidate_prefix((guild_id,))

    def invalidate_config(self, guild_id: int) -> None:
        self.guild_config.invalidate(guild_id)

    @property
    def hits(self) -> int:
        return sum(cache.hits for cache in self._all())

    @property
    def misses(self) -> int:
        return sum(cache.misses for cache in self._all())

    @property
    def size(self) -> int:
        return sum(len(cache) for cache in self._all())

    def _all(self) -> tuple[TTLCache[Any, Any], ...]:
        return (
            self.guild_config,
            self.ranks,
            self.permissions,
            self.room_owner,
            self.pulse,
            self.limit_bypass,
            self.automod_windows,
            self.member_joined,
            self.suspensions,
            self.showcase,
        )


def _matches_prefix(key: object, prefix: tuple[object, ...]) -> bool:
    if isinstance(key, tuple) and len(key) >= len(prefix) and key[: len(prefix)] == prefix:
        return True
    return len(prefix) == 1 and key == prefix[0]


def roles_changed(before_ids: Iterable[int], after_ids: Iterable[int]) -> bool:
    return set(before_ids) != set(after_ids)
