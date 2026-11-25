from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import ActivityBucket, ChannelDaily, MemberChannel, MemberDaily, MemberRecord, VoiceSession
from conductor.domain.activity import EventKind, PeriodKind, as_utc, day_bucket, month_bucket
from conductor.domain.insights import TENURE_BUCKETS, WEEKDAY_NAMES, age_days, tenure_bucket, weekday_name


@dataclass(slots=True)
class OverviewStats:
    humans: int = 0
    bots: int = 0
    boosters: int = 0
    left: int = 0
    joined_7d: int = 0
    joined_30d: int = 0
    median_tenure_days: int = 0
    tenure: list[tuple[str, int]] = field(default_factory=list)
    join_months: list[tuple[datetime, int]] = field(default_factory=list)
    join_weekdays: list[tuple[str, int]] = field(default_factory=list)
    top_message_days: Sequence[ActivityBucket] = field(default_factory=list)
    top_voice_days: Sequence[ActivityBucket] = field(default_factory=list)
    peak_hours: list[tuple[int, int]] = field(default_factory=list)
    top_channels: list[tuple[int, int]] = field(default_factory=list)
    unique_today: int = 0
    unique_7d: int = 0
    top_members: list[tuple[int, int]] = field(default_factory=list)


async def join_month_histogram(
    session: AsyncSession,
    guild_id: int,
    *,
    humans_only: bool = True,
) -> list[tuple[datetime, int]]:
    members = await _humans(session, guild_id, humans_only=humans_only, current_only=True)
    buckets: dict[datetime, int] = {}
    for member in members:
        key = month_bucket(member.first_joined_at)
        buckets[key] = buckets.get(key, 0) + 1
    return sorted(buckets.items(), key=lambda item: item[1], reverse=True)


async def top_buckets(
    session: AsyncSession,
    guild_id: int,
    *,
    period_kind: PeriodKind,
    event_kind: EventKind,
    limit: int = 10,
) -> Sequence[ActivityBucket]:
    stmt = (
        select(ActivityBucket)
        .where(
            ActivityBucket.guild_id == guild_id,
            ActivityBucket.period_kind == period_kind,
            ActivityBucket.event_kind == event_kind,
        )
        .order_by(ActivityBucket.count.desc())
        .limit(limit)
    )
    return (await session.scalars(stmt)).all()


async def hourly_profile(
    session: AsyncSession,
    guild_id: int,
    event_kind: EventKind,
) -> list[tuple[int, int]]:
    stmt = select(ActivityBucket).where(
        ActivityBucket.guild_id == guild_id,
        ActivityBucket.period_kind == PeriodKind.HOUR,
        ActivityBucket.event_kind == event_kind,
    )
    rows = (await session.scalars(stmt)).all()
    hours = dict.fromkeys(range(24), 0)
    for row in rows:
        hours[row.period_start.hour] += row.count
    return sorted(hours.items(), key=lambda item: item[1], reverse=True)


async def build_overview(session: AsyncSession, guild_id: int, now: datetime | None = None) -> OverviewStats:
    now = now or datetime.now(UTC)
    stats = OverviewStats()
    current = await _humans(session, guild_id, humans_only=True, current_only=True)
    all_humans = await _humans(session, guild_id, humans_only=True, current_only=False)
    stats.humans = len(current)
    stats.boosters = sum(1 for member in current if member.booster_since is not None)
    stats.bots = (
        await session.scalar(
            select(func.count())
            .select_from(MemberRecord)
            .where(
                MemberRecord.guild_id == guild_id,
                MemberRecord.currently_member.is_(True),
                MemberRecord.is_bot.is_(True),
            )
        )
        or 0
    )
    stats.left = sum(1 for member in all_humans if not member.currently_member)
    stats.joined_7d = sum(1 for member in current if age_days(member.first_joined_at, now) < 7)
    stats.joined_30d = sum(1 for member in current if age_days(member.first_joined_at, now) < 30)
    if current:
        tenures = sorted(age_days(member.first_joined_at, now) for member in current)
        stats.median_tenure_days = tenures[len(tenures) // 2]
    counts: Counter[str] = Counter()
    weekdays: Counter[str] = Counter()
    for member in current:
        counts[tenure_bucket(member.first_joined_at, now)] += 1
        weekdays[weekday_name(member.first_joined_at)] += 1
    stats.tenure = [(label, counts.get(label, 0)) for _, label in TENURE_BUCKETS]
    stats.join_weekdays = [(name, weekdays.get(name, 0)) for name in WEEKDAY_NAMES]
    stats.join_months = (await join_month_histogram(session, guild_id))[:8]
    stats.top_message_days = await top_buckets(
        session, guild_id, period_kind=PeriodKind.DAY, event_kind=EventKind.MESSAGE, limit=8
    )
    stats.top_voice_days = await top_buckets(
        session, guild_id, period_kind=PeriodKind.DAY, event_kind=EventKind.VOICE_MINUTE, limit=5
    )
    stats.peak_hours = await hourly_profile(session, guild_id, EventKind.MESSAGE)
    start_7d = now - timedelta(days=7)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    channel_rows = (
        await session.execute(
            select(ChannelDaily.channel_id, func.sum(ChannelDaily.count))
            .where(
                ChannelDaily.guild_id == guild_id,
                ChannelDaily.event_kind == EventKind.MESSAGE,
            )
            .group_by(ChannelDaily.channel_id)
            .order_by(desc(func.sum(ChannelDaily.count)))
            .limit(8)
        )
    ).all()
    stats.top_channels = [(int(channel_id), int(total or 0)) for channel_id, total in channel_rows]
    stats.unique_today = (
        await session.scalar(
            select(func.count())
            .select_from(MemberDaily)
            .where(
                MemberDaily.guild_id == guild_id,
                MemberDaily.day >= today,
                MemberDaily.messages > 0,
            )
        )
        or 0
    )
    stats.unique_7d = (
        await session.scalar(
            select(func.count(func.distinct(MemberDaily.user_id))).where(
                MemberDaily.guild_id == guild_id,
                MemberDaily.day >= start_7d.replace(hour=0, minute=0, second=0, microsecond=0),
                MemberDaily.messages > 0,
            )
        )
        or 0
    )
    top = (
        await session.execute(
            select(MemberRecord.user_id, MemberRecord.message_count)
            .where(
                MemberRecord.guild_id == guild_id,
                MemberRecord.is_bot.is_(False),
                MemberRecord.message_count > 0,
            )
            .order_by(MemberRecord.message_count.desc())
            .limit(8)
        )
    ).all()
    stats.top_members = [(int(user_id), int(count or 0)) for user_id, count in top]
    return stats


async def top_channels(
    session: AsyncSession,
    guild_id: int,
    days: int = 7,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> list[tuple[int, int, int]]:
    moment = as_utc(now or datetime.now(UTC))
    since = day_bucket(moment) - timedelta(days=max(1, int(days)))
    width = max(1, int(limit))
    message_rows = (
        await session.execute(
            select(ChannelDaily.channel_id, func.sum(ChannelDaily.count))
            .where(
                ChannelDaily.guild_id == guild_id,
                ChannelDaily.event_kind == EventKind.MESSAGE,
                ChannelDaily.day >= since,
            )
            .group_by(ChannelDaily.channel_id)
            .order_by(desc(func.sum(ChannelDaily.count)))
            .limit(width)
        )
    ).all()
    ranks = [(int(channel_id), int(total or 0)) for channel_id, total in message_rows]
    if not ranks:
        return []
    channel_ids = [channel_id for channel_id, _total in ranks]
    author_rows = (
        await session.execute(
            select(MemberChannel.channel_id, func.count(MemberChannel.user_id))
            .where(
                MemberChannel.guild_id == guild_id,
                MemberChannel.channel_id.in_(channel_ids),
                MemberChannel.last_at >= since,
            )
            .group_by(MemberChannel.channel_id)
        )
    ).all()
    authors = {int(channel_id): int(count or 0) for channel_id, count in author_rows}
    return [(channel_id, messages, authors.get(channel_id, 0)) for channel_id, messages in ranks]


async def voice_seconds(
    session: AsyncSession,
    guild_id: int,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> int:
    moment = as_utc(now or datetime.now(UTC))
    since = moment - timedelta(days=max(1, int(days)))
    rows = (
        await session.execute(
            select(VoiceSession.joined_at, VoiceSession.left_at).where(
                VoiceSession.guild_id == guild_id,
                or_(VoiceSession.left_at.is_(None), VoiceSession.left_at >= since),
            )
        )
    ).all()
    total = 0
    for joined_at, left_at in rows:
        start = max(as_utc(joined_at), since)
        end = moment if left_at is None else as_utc(left_at)
        if end > start:
            total += int((end - start).total_seconds())
    return total


async def _humans(
    session: AsyncSession,
    guild_id: int,
    *,
    humans_only: bool,
    current_only: bool,
) -> list[MemberRecord]:
    stmt = select(MemberRecord).where(MemberRecord.guild_id == guild_id)
    if humans_only:
        stmt = stmt.where(MemberRecord.is_bot.is_(False))
    if current_only:
        stmt = stmt.where(MemberRecord.currently_member.is_(True))
    return list((await session.scalars(stmt)).all())
