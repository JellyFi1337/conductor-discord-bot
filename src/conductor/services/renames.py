"""Единая точка channel.edit(name=): лимит 2/10 мин и 429."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import select

from conductor.db.models import RenameState
from conductor.db.session import session_scope
from conductor.domain.renames import RENAME_LIMIT, WINDOW_SECONDS, Deferred
from conductor.domain.traffic import CHANNEL_PATCH_TEMPLATE
from conductor.services.runtime import log_tick

log = structlog.get_logger("conductor.renames")

RETRY_JOB = "rename-retry"


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _retry_after(exc: BaseException) -> float | None:
    retry = getattr(exc, "retry_after", None)
    if retry is not None:
        return max(1.0, float(retry))
    if int(getattr(exc, "status", 0) or 0) == 429:
        return 60.0
    return None


def window_until(
    row: RenameState,
    now: datetime,
    *,
    reset_after: float | None = None,
    observed: bool = False,
) -> datetime | None:
    blocked = _aware(row.blocked_until)
    if blocked is not None and blocked > now:
        return blocked
    if observed:
        if reset_after is None or reset_after <= 0:
            return None
        return now + timedelta(seconds=float(reset_after))
    stamps: list[datetime] = []
    for raw in (row.renamed_at, row.previous_at):
        when = _aware(raw)
        if when is not None and now - when < timedelta(seconds=WINDOW_SECONDS):
            stamps.append(when)
    if len(stamps) >= RENAME_LIMIT:
        return min(stamps) + timedelta(seconds=WINDOW_SECONDS)
    return None


class RenameService:
    def __init__(self, bot: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._bot = bot
        self._clock = clock or (lambda: datetime.now(UTC))
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(30, RETRY_JOB, self.retry_due)

    def now(self) -> datetime:
        return self._clock()

    async def rename(self, channel: Any, name: str, *, key: str, reason: str = "") -> Deferred | None:
        wanted = str(name)
        if str(getattr(channel, "name", "") or "") == wanted:
            return None
        guild = getattr(channel, "guild", None)
        guild_id = int(getattr(guild, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        now = self.now()
        observed, reset_after = self._observed_wait(channel_id, now)
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, guild_id, channel_id, key)
            until = window_until(row, now, reset_after=reset_after, observed=observed)
            if until is not None:
                row.pending_name = wanted
                row.key = key
                self._schedule(guild_id, channel_id, until, now)
                return Deferred(until)
        try:
            await channel.edit(name=wanted, reason=reason)
        except (discord.HTTPException, discord.RateLimited) as exc:
            # RateLimited — не HTTPException: библиотека бросает его, когда 429 длиннее max_ratelimit_timeout.
            retry = _retry_after(exc)
            if retry is None:
                log.warning("rename_failed", guild_id=guild_id, channel_id=channel_id, error=str(exc))
                return None
            until = now + timedelta(seconds=retry)
            async with session_scope(self._bot.sessions) as session:
                row = await self._row(session, guild_id, channel_id, key)
                row.pending_name = wanted
                row.blocked_until = until
                row.key = key
            self._schedule(guild_id, channel_id, until, now)
            return Deferred(until)
        if hasattr(channel, "name"):
            channel.name = wanted
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, guild_id, channel_id, key)
            row.previous_at = row.renamed_at
            row.renamed_at = now
            row.pending_name = None
            row.blocked_until = None
            row.key = key
        return None

    async def retry_due(self) -> int:
        now = self.now()
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(RenameState).where(RenameState.pending_name.is_not(None)))).all()
            snapshot = []
            for row in rows:
                if not row.pending_name:
                    continue
                seen, reset_after = self._observed_wait(int(row.channel_id), now)
                if window_until(row, now, reset_after=reset_after, observed=seen) is None:
                    snapshot.append(
                        (int(row.guild_id), int(row.channel_id), str(row.pending_name), str(row.key))
                    )
        applied = 0
        gone: list[tuple[int, int]] = []
        for guild_id, channel_id, pending, key in snapshot:
            channel = self._channel(channel_id)
            if channel is None:
                # Канал удалён — иначе строка с pending_name перечитывается каждые 30 секунд вечно.
                gone.append((guild_id, channel_id))
                continue
            result = await self.rename(channel, pending, key=key)
            if result is None:
                applied += 1
        ready = getattr(self._bot, "is_ready", None)
        if gone and (not callable(ready) or ready()):
            async with session_scope(self._bot.sessions) as session:
                for guild_id, channel_id in gone:
                    row = await session.get(RenameState, (guild_id, channel_id))
                    if row is not None:
                        await session.delete(row)
        log_tick(log, "rename_retry", changed=applied + len(gone))
        return applied

    def _channel(self, channel_id: int) -> Any:
        getter = getattr(self._bot, "get_channel", None)
        if not callable(getter):
            return None
        return getter(channel_id)

    def _observed_wait(self, channel_id: int, now: datetime) -> tuple[bool, float | None]:
        observer = getattr(self._bot, "rate_observer", None)
        state_fn = getattr(observer, "state", None)
        if not callable(state_fn):
            return False, None
        bucket = state_fn(CHANNEL_PATCH_TEMPLATE, str(int(channel_id)))
        if bucket is None:
            return False, None
        reset = max(0.0, float(bucket.reset_after) - (now.timestamp() - float(bucket.at)))
        if int(bucket.remaining) > 0:
            return True, 0.0
        return True, reset

    def _schedule(self, guild_id: int, channel_id: int, until: datetime, now: datetime) -> None:
        queue = getattr(self._bot, "queue", None)
        later = getattr(queue, "schedule_later", None)
        if not callable(later):
            return
        delay = max(0.0, (until - now).total_seconds())
        later((RETRY_JOB, guild_id, channel_id), delay, self.retry_due)

    async def _row(self, session: Any, guild_id: int, channel_id: int, key: str) -> RenameState:
        row = await session.get(RenameState, (guild_id, channel_id))
        if row is not None:
            return row
        row = RenameState(guild_id=guild_id, channel_id=channel_id, key=key)
        session.add(row)
        await session.flush()
        return row
