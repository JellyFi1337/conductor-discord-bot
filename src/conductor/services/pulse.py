"""Сборка снимка пульса из кэша гильдии и уже собранной телеметрии."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    ActivityBucket,
    Gathering,
    MemberDaily,
    MemberRecord,
    PulseAlertSettings,
    RecruitLink,
    Ticket,
    VoiceSession,
    WatcherDuty,
)
from conductor.db.session import session_scope
from conductor.domain.activity import EventKind, PeriodKind, as_utc, day_bucket
from conductor.domain.pulse import (
    BOOST_LOOKBACK_HOURS,
    BOOST_SAMPLE_MIN_HOURS,
    AlertConfigVerdict,
    PulseAlertRules,
    PulseInputs,
    PulseSnapshot,
    census_age_hours,
    health_score,
    parse_authors_pct,
    parse_floor,
    peak_hour_of,
)
from conductor.domain.tickets import TicketState
from conductor.services.cache import CacheHub
from conductor.services.guild_config import get_or_create_config

TICKET_OPEN = (TicketState.OPEN.value, TicketState.CLAIMED.value)


def bump_pulse(bot: Any, guild_id: int, *, now: datetime | None = None) -> None:
    pulse = getattr(bot, "pulse", None)
    invalidate = getattr(pulse, "invalidate", None)
    if callable(invalidate):
        invalidate(int(guild_id), now=now)


class PulseService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._assembled_at: dict[int, datetime] = {}
        self._invalidated_at: dict[int, datetime] = {}
        self._band: dict[int, str] = {}
        self._fallback_cache: Any | None = None
        self._rules: dict[int, PulseAlertRules] = {}
        self._booster_log: dict[int, list[tuple[datetime, int]]] = {}

    def invalidate(self, guild_id: int, *, now: datetime | None = None) -> None:
        moment = as_utc(now or datetime.now(UTC))
        gid = int(guild_id)
        self._cache().invalidate(gid)
        self._invalidated_at[gid] = moment

    def last_assembled_at(self, guild_id: int) -> datetime | None:
        return self._assembled_at.get(int(guild_id))

    def last_invalidated_at(self, guild_id: int) -> datetime | None:
        return self._invalidated_at.get(int(guild_id))

    def last_band(self, guild_id: int) -> str | None:
        return self._band.get(int(guild_id))

    def last_alert_rules(self, guild_id: int) -> PulseAlertRules:
        return self._rules.get(int(guild_id), PulseAlertRules())

    async def alert_rules(self, guild_id: int) -> PulseAlertRules:
        async with session_scope(self._bot.sessions) as session:
            rules = await _load_rules(session, int(guild_id))
        self._rules[int(guild_id)] = rules
        return rules

    async def set_alerts(
        self,
        guild_id: int,
        *,
        enabled: bool,
        voice_floor: int | None = None,
        leave_floor: int | None = None,
        authors_pct: int | None = None,
        join_spike_floor: int | None = None,
    ) -> tuple[AlertConfigVerdict, PulseAlertRules]:
        current = await self.alert_rules(guild_id)
        voice, voice_ok = parse_floor(voice_floor, current.voice_floor)
        if not voice_ok or voice is None:
            return AlertConfigVerdict.BAD_VOICE, current
        leaves, leaves_ok = parse_floor(leave_floor, current.leave_floor)
        if not leaves_ok or leaves is None:
            return AlertConfigVerdict.BAD_LEAVES, current
        if authors_pct is None:
            authors, authors_ok = current.authors_pct, True
        else:
            authors, authors_ok = parse_authors_pct(authors_pct)
        if not authors_ok or authors is None:
            return AlertConfigVerdict.BAD_AUTHORS, current
        joins, joins_ok = parse_floor(join_spike_floor, current.join_spike_floor)
        if not joins_ok or joins is None:
            return AlertConfigVerdict.BAD_JOINS, current
        rules = PulseAlertRules(
            enabled=bool(enabled),
            voice_floor=voice,
            leave_floor=leaves,
            authors_pct=authors,
            join_spike_floor=joins,
        )
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PulseAlertSettings, int(guild_id))
            if row is None:
                session.add(
                    PulseAlertSettings(
                        guild_id=int(guild_id),
                        enabled=rules.enabled,
                        voice_floor=rules.voice_floor,
                        leave_floor=rules.leave_floor,
                        authors_pct=rules.authors_pct,
                        join_spike_floor=rules.join_spike_floor,
                    )
                )
            else:
                row.enabled = rules.enabled
                row.voice_floor = rules.voice_floor
                row.leave_floor = rules.leave_floor
                row.authors_pct = rules.authors_pct
                row.join_spike_floor = rules.join_spike_floor
        self._rules[int(guild_id)] = rules
        return AlertConfigVerdict.OK, rules

    def has_presence_intent(self) -> bool:
        intents = getattr(self._bot, "intents", None)
        return bool(getattr(intents, "presences", False))

    async def snapshot(self, guild: Any, *, now: datetime | None = None) -> PulseSnapshot:
        moment = as_utc(now or datetime.now(UTC))
        guild_id = int(guild.id)
        cached = self._cache().get(guild_id)
        if isinstance(cached, PulseSnapshot):
            return PulseSnapshot(
                inputs=cached.inputs,
                report=cached.report,
                assembled_at=cached.assembled_at,
                from_cache=True,
            )
        inputs = await self._collect(guild, moment)
        report = health_score(inputs, moment)
        snap = PulseSnapshot(inputs=inputs, report=report, assembled_at=moment, from_cache=False)
        self._cache().set(guild_id, snap)
        self._assembled_at[guild_id] = moment
        self._band[guild_id] = report.band.value
        return snap

    async def _collect(self, guild: Any, now: datetime) -> PulseInputs:
        guild_id = int(guild.id)
        live = _live_counts(guild, has_presence=self.has_presence_intent())
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, guild_id)
            stored = await _stored_counts(session, guild_id)
            activity = await _activity(session, guild_id, now)
            tickets_open = await _tickets_open(session, guild_id)
            duty = await _duty_count(session, guild_id)
            gathering = await _next_gathering(session, guild_id, now)
            rules = await _load_rules(session, guild_id)
        self._rules[guild_id] = rules
        humans, bots, boosters = _pick_counts(live, stored)
        self._remember_boosters(guild_id, boosters, now)
        coded_7d, vanity_7d, unknown_7d = await self._inflow_7d(guild_id, guild, now)
        return PulseInputs(
            humans=humans,
            bots=bots,
            boosters=boosters,
            premium_tier=int(getattr(guild, "premium_tier", 0) or 0),
            voice_now=live.voice_now,
            online=live.online,
            unique_authors_24h=activity.unique_authors_24h,
            messages_24h=activity.messages_24h,
            messages_7d_avg=activity.messages_7d_avg,
            voice_minutes_24h=activity.voice_minutes_24h,
            joins_7d=activity.joins_7d,
            leaves_7d=activity.leaves_7d,
            tickets_open=tickets_open,
            duty_watchers=duty,
            next_gathering_at=None if gathering is None else gathering[0],
            next_gathering_title=None if gathering is None else gathering[1],
            next_gathering_id=None if gathering is None else gathering[2],
            has_presence_intent=self.has_presence_intent(),
            census_age_hours=census_age_hours(getattr(config, "census_at", None), now),
            has_message_history=activity.has_message_history,
            has_voice_sessions=activity.has_voice_sessions,
            joins_7d_coded=coded_7d,
            joins_7d_vanity=vanity_7d,
            joins_7d_unknown=unknown_7d,
            joins_24h=activity.joins_24h,
            leaves_24h=activity.leaves_24h,
            authors_7d=activity.authors_7d,
            peak_hour=activity.peak_hour,
            peak_voice_7d=activity.peak_voice_7d,
            boosters_24h_ago=self._boosters_24h_ago(guild_id, now),
        )

    def _remember_boosters(self, guild_id: int, boosters: int, now: datetime) -> None:
        log = self._booster_log.setdefault(int(guild_id), [])
        log.append((now, int(boosters)))
        cutoff = now - timedelta(hours=48)
        self._booster_log[int(guild_id)] = [(stamp, count) for stamp, count in log if stamp >= cutoff]

    def _boosters_24h_ago(self, guild_id: int, now: datetime) -> int | None:
        target = now - timedelta(hours=BOOST_LOOKBACK_HOURS)
        old = [
            (stamp, count)
            for stamp, count in self._booster_log.get(int(guild_id), ())
            if stamp <= now - timedelta(hours=BOOST_SAMPLE_MIN_HOURS)
        ]
        if not old:
            return None
        _stamp, count = min(old, key=lambda item: abs((item[0] - target).total_seconds()))
        return count

    async def _inflow_7d(self, guild_id: int, guild: Any, now: datetime) -> tuple[int, int, int]:
        vanity = getattr(guild, "vanity_url_code", None)
        vanity_code = None if not vanity else str(vanity)
        since = as_utc(now) - timedelta(days=7)
        async with session_scope(self._bot.sessions) as session:
            people = (
                await session.scalars(
                    select(MemberRecord).where(
                        MemberRecord.guild_id == int(guild_id),
                        MemberRecord.is_bot.is_(False),
                        MemberRecord.first_joined_at >= since,
                    )
                )
            ).all()
            recruit_codes = set(
                (await session.scalars(select(RecruitLink.code).where(RecruitLink.guild_id == int(guild_id)))).all()
            )
        coded = vanity_n = unknown = 0
        for row in people:
            code = row.invite_code
            if vanity_code and code == vanity_code:
                vanity_n += 1
            elif code and code in recruit_codes:
                coded += 1
            else:
                unknown += 1
        return coded, vanity_n, unknown

    def _cache(self) -> Any:
        caches = getattr(self._bot, "caches", None)
        pulse = getattr(caches, "pulse", None)
        if pulse is not None:
            return pulse
        if self._fallback_cache is None:
            self._fallback_cache = CacheHub().pulse
        return self._fallback_cache


@dataclass
class _LiveCounts:
    humans: int
    bots: int
    boosters: int
    voice_now: int
    online: int | None
    populated: bool


@dataclass
class _StoredCounts:
    humans: int
    bots: int
    boosters: int


@dataclass
class _Activity:
    unique_authors_24h: int
    messages_24h: int
    messages_7d_avg: float
    voice_minutes_24h: int
    joins_7d: int
    leaves_7d: int
    has_message_history: bool
    has_voice_sessions: bool
    joins_24h: int = 0
    leaves_24h: int = 0
    authors_7d: tuple[int, ...] = ()
    peak_hour: int | None = None
    peak_voice_7d: tuple[int, ...] = ()


def _live_counts(guild: Any, *, has_presence: bool) -> _LiveCounts:
    members = tuple(getattr(guild, "members", ()) or ())
    humans = sum(1 for member in members if not bool(getattr(member, "bot", False)))
    bots = sum(1 for member in members if bool(getattr(member, "bot", False)))
    boosters = int(getattr(guild, "premium_subscription_count", 0) or 0)
    if boosters == 0:
        boosters = sum(1 for member in members if getattr(member, "premium_since", None) is not None)
    voice_now = 0
    for channel in (
        *(getattr(guild, "voice_channels", ()) or ()),
        *(getattr(guild, "stage_channels", ()) or ()),
    ):
        for member in getattr(channel, "members", ()) or ():
            if not bool(getattr(member, "bot", False)):
                voice_now += 1
    online = None
    if has_presence:
        online = sum(1 for member in members if not bool(getattr(member, "bot", False)) and not _offline(member))
    return _LiveCounts(
        humans=humans,
        bots=bots,
        boosters=boosters,
        voice_now=voice_now,
        online=online,
        populated=bool(members),
    )


def _offline(member: Any) -> bool:
    status = getattr(member, "status", None)
    if status is None:
        return True
    value = getattr(status, "value", status)
    return str(value).lower() == "offline"


def _pick_counts(live: _LiveCounts, stored: _StoredCounts) -> tuple[int, int, int]:
    if live.populated:
        return live.humans, live.bots, live.boosters
    return stored.humans, stored.bots, stored.boosters


async def _stored_counts(session: AsyncSession, guild_id: int) -> _StoredCounts:
    humans = (
        await session.scalar(
            select(func.count())
            .select_from(MemberRecord)
            .where(
                MemberRecord.guild_id == guild_id,
                MemberRecord.currently_member.is_(True),
                MemberRecord.is_bot.is_(False),
            )
        )
        or 0
    )
    bots = (
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
    boosters = (
        await session.scalar(
            select(func.count())
            .select_from(MemberRecord)
            .where(
                MemberRecord.guild_id == guild_id,
                MemberRecord.currently_member.is_(True),
                MemberRecord.is_bot.is_(False),
                MemberRecord.booster_since.is_not(None),
            )
        )
        or 0
    )
    return _StoredCounts(humans=int(humans), bots=int(bots), boosters=int(boosters))


async def _activity(session: AsyncSession, guild_id: int, now: datetime) -> _Activity:
    day_from = day_bucket(now - timedelta(hours=24))
    hour_from = now - timedelta(hours=24)
    week_from = day_bucket(now) - timedelta(days=7)
    unique = (
        await session.scalar(
            select(func.count(func.distinct(MemberDaily.user_id))).where(
                MemberDaily.guild_id == guild_id,
                MemberDaily.day >= day_from,
                MemberDaily.messages > 0,
            )
        )
        or 0
    )
    messages_24h = await _bucket_sum(session, guild_id, EventKind.MESSAGE, PeriodKind.HOUR, since=hour_from)
    messages_7d = await _bucket_sum(session, guild_id, EventKind.MESSAGE, PeriodKind.DAY, since=week_from)
    voice_24h = await _bucket_sum(session, guild_id, EventKind.VOICE_MINUTE, PeriodKind.HOUR, since=hour_from)
    joins = await _bucket_sum(session, guild_id, EventKind.JOIN, PeriodKind.DAY, since=week_from)
    leaves = await _bucket_sum(session, guild_id, EventKind.LEAVE, PeriodKind.DAY, since=week_from)
    joins_24h = await _bucket_sum(session, guild_id, EventKind.JOIN, PeriodKind.HOUR, since=hour_from)
    leaves_24h = await _bucket_sum(session, guild_id, EventKind.LEAVE, PeriodKind.HOUR, since=hour_from)
    authors_7d = await _authors_7d(session, guild_id, now)
    peak_hour = peak_hour_of(await _hourly_totals(session, guild_id, EventKind.MESSAGE, since=week_from))
    peak_voice = ()
    if peak_hour is not None:
        peak_voice = await _hour_series(session, guild_id, EventKind.VOICE_MINUTE, peak_hour, now)
    history = await _bucket_exists(session, guild_id, EventKind.MESSAGE, since=week_from)
    closed = await session.scalar(
        select(VoiceSession.id)
        .where(
            VoiceSession.guild_id == guild_id,
            VoiceSession.left_at.is_not(None),
            VoiceSession.left_at >= week_from,
        )
        .limit(1)
    )
    return _Activity(
        unique_authors_24h=int(unique),
        messages_24h=int(messages_24h),
        messages_7d_avg=float(messages_7d) / 7.0,
        voice_minutes_24h=int(voice_24h),
        joins_7d=int(joins),
        leaves_7d=int(leaves),
        has_message_history=history,
        has_voice_sessions=closed is not None,
        joins_24h=int(joins_24h),
        leaves_24h=int(leaves_24h),
        authors_7d=authors_7d,
        peak_hour=peak_hour,
        peak_voice_7d=peak_voice,
    )


async def _bucket_sum(
    session: AsyncSession,
    guild_id: int,
    event: EventKind,
    period: PeriodKind,
    *,
    since: datetime,
) -> int:
    value = await session.scalar(
        select(func.coalesce(func.sum(ActivityBucket.count), 0)).where(
            ActivityBucket.guild_id == guild_id,
            ActivityBucket.event_kind == event.value,
            ActivityBucket.period_kind == period.value,
            ActivityBucket.period_start >= since,
        )
    )
    return int(value or 0)


async def _bucket_exists(
    session: AsyncSession,
    guild_id: int,
    event: EventKind,
    *,
    since: datetime,
) -> bool:
    found = await session.scalar(
        select(ActivityBucket.id)
        .where(
            ActivityBucket.guild_id == guild_id,
            ActivityBucket.event_kind == event.value,
            ActivityBucket.period_start >= since,
            ActivityBucket.count > 0,
        )
        .limit(1)
    )
    return found is not None


async def _tickets_open(session: AsyncSession, guild_id: int) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.guild_id == guild_id,
            Ticket.state.in_(TICKET_OPEN),
        )
    )
    return int(value or 0)


async def _duty_count(session: AsyncSession, guild_id: int) -> int:
    value = await session.scalar(select(func.count()).select_from(WatcherDuty).where(WatcherDuty.guild_id == guild_id))
    return int(value or 0)


async def _next_gathering(session: AsyncSession, guild_id: int, now: datetime) -> tuple[datetime, str, int] | None:
    row = await session.scalar(
        select(Gathering)
        .where(
            Gathering.guild_id == guild_id,
            Gathering.cancelled_at.is_(None),
            Gathering.starts_at >= now,
        )
        .order_by(Gathering.starts_at.asc())
        .limit(1)
    )
    if row is None:
        return None
    return as_utc(row.starts_at), str(row.title), int(row.id)


async def _load_rules(session: AsyncSession, guild_id: int) -> PulseAlertRules:
    row = await session.get(PulseAlertSettings, int(guild_id))
    if row is None:
        return PulseAlertRules()
    return PulseAlertRules(
        enabled=bool(row.enabled),
        voice_floor=int(row.voice_floor),
        leave_floor=int(row.leave_floor),
        authors_pct=int(row.authors_pct),
        join_spike_floor=int(getattr(row, "join_spike_floor", 15) or 15),
    )


async def _authors_7d(session: AsyncSession, guild_id: int, now: datetime) -> tuple[int, ...]:
    start = day_bucket(now) - timedelta(days=6)
    rows = (
        await session.execute(
            select(MemberDaily.day, func.count(func.distinct(MemberDaily.user_id)))
            .where(
                MemberDaily.guild_id == guild_id,
                MemberDaily.day >= start,
                MemberDaily.messages > 0,
            )
            .group_by(MemberDaily.day)
        )
    ).all()
    by_day = {as_utc(day): int(count) for day, count in rows}
    return tuple(by_day.get(start + timedelta(days=offset), 0) for offset in range(7))


async def _hourly_totals(
    session: AsyncSession,
    guild_id: int,
    event: EventKind,
    *,
    since: datetime,
) -> dict[int, int]:
    rows = (
        await session.scalars(
            select(ActivityBucket).where(
                ActivityBucket.guild_id == guild_id,
                ActivityBucket.period_kind == PeriodKind.HOUR,
                ActivityBucket.event_kind == event.value,
                ActivityBucket.period_start >= since,
            )
        )
    ).all()
    hours = dict.fromkeys(range(24), 0)
    for row in rows:
        hours[as_utc(row.period_start).hour] += int(row.count)
    return hours


async def _hour_series(
    session: AsyncSession,
    guild_id: int,
    event: EventKind,
    hour: int,
    now: datetime,
) -> tuple[int, ...]:
    start = day_bucket(now) - timedelta(days=6)
    rows = (
        await session.scalars(
            select(ActivityBucket).where(
                ActivityBucket.guild_id == guild_id,
                ActivityBucket.period_kind == PeriodKind.HOUR,
                ActivityBucket.event_kind == event.value,
                ActivityBucket.period_start >= start,
            )
        )
    ).all()
    days: dict[datetime, int] = {}
    for row in rows:
        stamp = as_utc(row.period_start)
        if stamp.hour != int(hour):
            continue
        day = day_bucket(stamp)
        days[day] = days.get(day, 0) + int(row.count)
    return tuple(days.get(start + timedelta(days=offset), 0) for offset in range(7))
