"""Persistent voice session accounting.

The logging cog only draws the card. This tracker is the source of truth for
seconds: open/close rows in `voice_sessions`, restore from current voice_states.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conductor.db.models import VoiceSession
from conductor.db.session import session_scope
from conductor.domain.voice import (
    LiveVoice,
    OpenVoiceSession,
    reconcile_sessions,
    session_seconds,
)
from conductor.services.activity import ActivityCollector


def _row_as_open(row: VoiceSession) -> OpenVoiceSession:
    return OpenVoiceSession(
        id=row.id,
        guild_id=row.guild_id,
        user_id=row.user_id,
        channel_id=row.channel_id,
        joined_at=row.joined_at,
        last_event_at=row.last_event_at,
    )


class VoiceSessionTracker:
    def __init__(self, collector: ActivityCollector) -> None:
        self._collector = collector
        self._open: dict[tuple[int, int], datetime] = {}
        self._lock = asyncio.Lock()

    def opened_at(self, guild_id: int, user_id: int) -> datetime | None:
        return self._open.get((guild_id, user_id))

    def forget_memory(self, guild_id: int, user_id: int) -> None:
        self._open.pop((guild_id, user_id), None)

    async def restore(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        live: Sequence[LiveVoice],
        now: datetime,
    ) -> None:
        async with self._lock:
            async with session_scope(factory) as session:
                rows = (
                    await session.scalars(
                        select(VoiceSession).where(
                            VoiceSession.guild_id == guild_id,
                            VoiceSession.left_at.is_(None),
                        )
                    )
                ).all()
                plan = reconcile_sessions(tuple(_row_as_open(row) for row in rows), tuple(live), now)
                by_id = {row.id: row for row in rows}
                credited: list[tuple[int, int, datetime, int]] = []
                for action in plan.close:
                    row = by_id.get(action.session.id) if action.session.id is not None else None
                    if row is None:
                        continue
                    row.left_at = action.left_at
                    row.duration_seconds = action.seconds
                    row.last_event_at = action.left_at
                    credited.append((row.guild_id, row.user_id, row.joined_at, action.seconds))
                    self._open.pop((row.guild_id, row.user_id), None)
                for action in plan.keep:
                    row = by_id.get(action.session.id) if action.session.id is not None else None
                    if row is None:
                        continue
                    row.channel_id = action.channel_id
                    row.last_event_at = action.last_event_at
                    self._open[(row.guild_id, row.user_id)] = row.joined_at
                for action in plan.open:
                    session.add(
                        VoiceSession(
                            guild_id=action.live.guild_id,
                            user_id=action.live.user_id,
                            channel_id=action.live.channel_id,
                            joined_at=action.joined_at,
                            left_at=None,
                            duration_seconds=None,
                            last_event_at=action.joined_at,
                        )
                    )
                    self._open[(action.live.guild_id, action.live.user_id)] = action.joined_at
            for guild_id_, user_id, joined_at, seconds in credited:
                self._collector.note_voice_seconds(guild_id_, user_id, joined_at, seconds)

    async def apply(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        user_id: int,
        before_channel_id: int | None,
        after_channel_id: int | None,
        now: datetime,
    ) -> int | None:
        async with self._lock:
            if before_channel_id is None and after_channel_id is None:
                return None
            if before_channel_id is None and after_channel_id is not None:
                await self._open_session(factory, guild_id, user_id, after_channel_id, now)
                return None
            if before_channel_id is not None and after_channel_id is None:
                return await self._close_session(factory, guild_id, user_id, now)
            if before_channel_id == after_channel_id:
                await self._touch(factory, guild_id, user_id, now)
                return None
            seconds = await self._close_session(factory, guild_id, user_id, now)
            if after_channel_id is not None:
                await self._open_session(factory, guild_id, user_id, after_channel_id, now)
            return seconds

    async def close_member(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        user_id: int,
        now: datetime,
    ) -> int | None:
        async with self._lock:
            return await self._close_session(factory, guild_id, user_id, now)

    async def _open_session(
        self,
        factory: async_sessionmaker[AsyncSession],
        guild_id: int,
        user_id: int,
        channel_id: int,
        now: datetime,
    ) -> None:
        async with session_scope(factory) as session:
            row = await _open_row(session, guild_id, user_id)
            if row is not None:
                row.channel_id = channel_id
                row.last_event_at = now
                self._open[(guild_id, user_id)] = row.joined_at
                return
            session.add(
                VoiceSession(
                    guild_id=guild_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    joined_at=now,
                    left_at=None,
                    duration_seconds=None,
                    last_event_at=now,
                )
            )
            self._open[(guild_id, user_id)] = now

    async def _close_session(
        self,
        factory: async_sessionmaker[AsyncSession],
        guild_id: int,
        user_id: int,
        now: datetime,
    ) -> int | None:
        seconds: int | None = None
        joined_at: datetime | None = None
        async with session_scope(factory) as session:
            row = await _open_row(session, guild_id, user_id)
            if row is None:
                self._open.pop((guild_id, user_id), None)
                return None
            seconds = session_seconds(row.joined_at, now)
            row.left_at = now
            row.duration_seconds = seconds
            row.last_event_at = now
            joined_at = row.joined_at
            self._open.pop((guild_id, user_id), None)
        if seconds is not None and joined_at is not None:
            self._collector.note_voice_seconds(guild_id, user_id, joined_at, seconds)
        return seconds

    async def _touch(
        self,
        factory: async_sessionmaker[AsyncSession],
        guild_id: int,
        user_id: int,
        now: datetime,
    ) -> None:
        async with session_scope(factory) as session:
            row = await _open_row(session, guild_id, user_id)
            if row is None:
                return
            row.last_event_at = now
            self._open[(guild_id, user_id)] = row.joined_at


async def _open_row(session: AsyncSession, guild_id: int, user_id: int) -> VoiceSession | None:
    rows = (
        await session.scalars(
            select(VoiceSession)
            .where(
                VoiceSession.guild_id == guild_id,
                VoiceSession.user_id == user_id,
                VoiceSession.left_at.is_(None),
            )
            .order_by(VoiceSession.joined_at.asc(), VoiceSession.id.asc())
        )
    ).all()
    if not rows:
        return None
    keep, *dupes = rows
    for extra in dupes:
        extra.left_at = extra.last_event_at
        extra.duration_seconds = session_seconds(extra.joined_at, extra.last_event_at)
    return keep
