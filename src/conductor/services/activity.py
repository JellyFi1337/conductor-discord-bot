from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conductor.db.models import ActivityBucket, ChannelDaily, MemberDaily, MemberRecord
from conductor.db.session import session_scope
from conductor.domain.activity import EventKind, PeriodKind, day_bucket, hour_bucket, month_bucket


class ActivityCollector:
    """Buffer activity increments in memory and flush them in batches."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, datetime, str, str], int] = defaultdict(int)
        self._member_msgs: dict[tuple[int, int, datetime], int] = defaultdict(int)
        self._member_voice: dict[tuple[int, int, datetime], int] = defaultdict(int)
        self._member_react: dict[tuple[int, int, datetime], int] = defaultdict(int)
        self._channel: dict[tuple[int, int, datetime, str], int] = defaultdict(int)
        self._last_seen: dict[tuple[int, int], datetime] = {}
        self._last_message: dict[tuple[int, int], datetime] = {}
        self._lock = asyncio.Lock()

    def bump(
        self,
        guild_id: int,
        moment: datetime,
        event_kind: EventKind,
        amount: int = 1,
        *,
        channel_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        if amount <= 0:
            return
        self._pending[(guild_id, hour_bucket(moment), PeriodKind.HOUR, event_kind)] += amount
        self._pending[(guild_id, day_bucket(moment), PeriodKind.DAY, event_kind)] += amount
        self._pending[(guild_id, month_bucket(moment), PeriodKind.MONTH, event_kind)] += amount
        day = day_bucket(moment)
        if channel_id is not None:
            self._channel[(guild_id, channel_id, day, event_kind)] += amount
        if user_id is None:
            return
        if event_kind is EventKind.MESSAGE:
            self._member_msgs[(guild_id, user_id, day)] += amount
            self._last_message[(guild_id, user_id)] = moment
            self._last_seen[(guild_id, user_id)] = moment
        elif event_kind is EventKind.VOICE_MINUTE:
            self._member_voice[(guild_id, user_id, day)] += amount * 60
            self._last_seen[(guild_id, user_id)] = moment
        elif event_kind is EventKind.REACTION:
            self._member_react[(guild_id, user_id, day)] += amount
            self._last_seen[(guild_id, user_id)] = moment

    def note_voice_seconds(self, guild_id: int, user_id: int, moment: datetime, seconds: int) -> None:
        if seconds < 15:
            return
        minutes = max(1, seconds // 60)
        self.bump(guild_id, moment, EventKind.VOICE_MINUTE, minutes)
        day = day_bucket(moment)
        self._member_voice[(guild_id, user_id, day)] += seconds
        self._last_seen[(guild_id, user_id)] = moment

    async def flush(self, factory: async_sessionmaker[AsyncSession]) -> int:
        async with self._lock:
            buckets = dict(self._pending)
            member_msgs = dict(self._member_msgs)
            member_voice = dict(self._member_voice)
            member_react = dict(self._member_react)
            channels = dict(self._channel)
            last_seen = dict(self._last_seen)
            last_message = dict(self._last_message)
            self._pending.clear()
            self._member_msgs.clear()
            self._member_voice.clear()
            self._member_react.clear()
            self._channel.clear()
            self._last_seen.clear()
            self._last_message.clear()
        total = len(buckets) + len(member_msgs) + len(member_voice) + len(member_react) + len(channels)
        if total == 0:
            return 0
        async with session_scope(factory) as session:
            for (guild_id, period_start, period_kind, event_kind), amount in buckets.items():
                await _increment_bucket(session, guild_id, period_start, period_kind, event_kind, amount)
            for (guild_id, channel_id, day, event_kind), amount in channels.items():
                await _increment_channel(session, guild_id, channel_id, day, event_kind, amount)
            keys = set(member_msgs) | set(member_voice) | set(member_react)
            for key in keys:
                guild_id, user_id, day = key
                await _increment_member_daily(
                    session,
                    guild_id=guild_id,
                    user_id=user_id,
                    day=day,
                    messages=member_msgs.get(key, 0),
                    voice_seconds=member_voice.get(key, 0),
                    reactions=member_react.get(key, 0),
                    last_seen=last_seen.get((guild_id, user_id)),
                    last_message=last_message.get((guild_id, user_id)),
                )
        return total


async def _increment_bucket(
    session: AsyncSession,
    guild_id: int,
    period_start: datetime,
    period_kind: str,
    event_kind: str,
    amount: int,
) -> None:
    stmt = select(ActivityBucket).where(
        ActivityBucket.guild_id == guild_id,
        ActivityBucket.period_start == period_start,
        ActivityBucket.period_kind == period_kind,
        ActivityBucket.event_kind == event_kind,
    )
    row = await session.scalar(stmt)
    if row is None:
        session.add(
            ActivityBucket(
                guild_id=guild_id,
                period_start=period_start,
                period_kind=period_kind,
                event_kind=event_kind,
                count=amount,
            )
        )
        return
    row.count += amount


async def _increment_channel(
    session: AsyncSession,
    guild_id: int,
    channel_id: int,
    day: datetime,
    event_kind: str,
    amount: int,
) -> None:
    stmt = select(ChannelDaily).where(
        ChannelDaily.guild_id == guild_id,
        ChannelDaily.channel_id == channel_id,
        ChannelDaily.day == day,
        ChannelDaily.event_kind == event_kind,
    )
    row = await session.scalar(stmt)
    if row is None:
        session.add(
            ChannelDaily(
                guild_id=guild_id,
                channel_id=channel_id,
                day=day,
                event_kind=event_kind,
                count=amount,
            )
        )
        return
    row.count += amount


async def _increment_member_daily(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    day: datetime,
    messages: int,
    voice_seconds: int,
    reactions: int,
    last_seen: datetime | None = None,
    last_message: datetime | None = None,
) -> None:
    stmt = select(MemberDaily).where(
        MemberDaily.guild_id == guild_id,
        MemberDaily.user_id == user_id,
        MemberDaily.day == day,
    )
    row = await session.scalar(stmt)
    if row is None:
        session.add(
            MemberDaily(
                guild_id=guild_id,
                user_id=user_id,
                day=day,
                messages=messages,
                voice_seconds=voice_seconds,
                reactions=reactions,
            )
        )
    else:
        row.messages += messages
        row.voice_seconds += voice_seconds
        row.reactions += reactions
    member = await session.scalar(
        select(MemberRecord).where(
            MemberRecord.guild_id == guild_id,
            MemberRecord.user_id == user_id,
        )
    )
    if member is None:
        return
    member.voice_seconds = (member.voice_seconds or 0) + voice_seconds
    if last_message is not None:
        member.last_message_at = last_message
    if last_seen is not None:
        member.last_seen_at = last_seen
