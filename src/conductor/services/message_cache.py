from __future__ import annotations

import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from conductor.domain.privacy import CACHE_TTL_SECONDS
from conductor.domain.records import CachedMessage

__all__ = ["CachedMessage", "MESSAGE_CACHE_TTL", "MessageCache"]

MESSAGE_CACHE_TTL = float(CACHE_TTL_SECONDS)


@dataclass(slots=True)
class _Entry:
    message: CachedMessage
    expires_at: float


class MessageCache:
    """Тексты сообщений: раздел по гильдиям, TTL, общий потолок на процесс."""

    def __init__(
        self,
        maxlen: int = 20_000,
        *,
        ttl: float = MESSAGE_CACHE_TTL,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._maxlen = max(0, maxlen)
        self._ttl = ttl
        self._clock = clock or time.monotonic
        self._guilds: dict[int, OrderedDict[int, _Entry]] = defaultdict(OrderedDict)
        self._index: dict[int, int] = {}
        self.hits = 0
        self.misses = 0

    def store(self, message: CachedMessage) -> None:
        if self._maxlen <= 0:
            return
        self._purge_expired()
        self._drop(message.id)
        bucket = self._guilds[message.guild_id]
        bucket[message.id] = _Entry(message=message, expires_at=self._clock() + self._ttl)
        bucket.move_to_end(message.id)
        self._index[message.id] = message.guild_id
        self._enforce_cap()

    def get(self, message_id: int) -> CachedMessage | None:
        found = self._alive(message_id)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        bucket = self._guilds[found.guild_id]
        bucket.move_to_end(message_id)
        return found

    def pop(self, message_id: int) -> CachedMessage | None:
        found = self._alive(message_id)
        self._drop(message_id)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return found

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._index)

    def size_for(self, guild_id: int) -> int:
        self._purge_expired()
        return len(self._guilds.get(guild_id, ()))

    def drop_author(self, guild_id: int, user_id: int) -> int:
        bucket = self._guilds.get(int(guild_id))
        if not bucket:
            return 0
        stale = [message_id for message_id, entry in bucket.items() if entry.message.author_id == int(user_id)]
        for message_id in stale:
            self._drop(message_id)
        return len(stale)

    def _alive(self, message_id: int) -> CachedMessage | None:
        guild_id = self._index.get(message_id)
        if guild_id is None:
            return None
        entry = self._guilds[guild_id].get(message_id)
        if entry is None:
            self._index.pop(message_id, None)
            return None
        if entry.expires_at <= self._clock():
            self._drop(message_id)
            return None
        return entry.message

    def _drop(self, message_id: int) -> None:
        guild_id = self._index.pop(message_id, None)
        if guild_id is None:
            return
        bucket = self._guilds.get(guild_id)
        if bucket is None:
            return
        bucket.pop(message_id, None)
        if not bucket:
            self._guilds.pop(guild_id, None)

    def _purge_expired(self) -> None:
        now = self._clock()
        stale = [
            message_id
            for guild_id, bucket in self._guilds.items()
            for message_id, entry in bucket.items()
            if entry.expires_at <= now
        ]
        for message_id in stale:
            self._drop(message_id)

    def _enforce_cap(self) -> None:
        while len(self._index) > self._maxlen:
            guild_id = max(self._guilds, key=lambda gid: len(self._guilds[gid]))
            bucket = self._guilds[guild_id]
            message_id, _entry = bucket.popitem(last=False)
            self._index.pop(message_id, None)
            if not bucket:
                self._guilds.pop(guild_id, None)
