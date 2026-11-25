"""Отчёт по следящим, приостановка и еженедельный дайджест."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    AutomodHit,
    GuildConfig,
    ModCase,
    ModEvidence,
    ModSettings,
    StaffAction,
    Ticket,
    TicketCategory,
    WatcherSuspension,
)
from conductor.db.session import session_scope
from conductor.domain.moderation import EVIDENCE_REMIND_HOURS_DEFAULT
from conductor.domain.staff_feed import ACTION_QUOTA_DENIED, ACTION_WATCH_LIFT, ACTION_WATCH_NOTE, ACTION_WATCH_SUSPEND
from conductor.domain.tickets import WORKING_STATES, CategoryKind, Outcome, TicketState
from conductor.domain.watchers import (
    ROSTER_FEED_LIMIT,
    UNANSWERED_AFTER,
    StaffActionView,
    SuspensionView,
    WatcherMetrics,
    can_suspend,
    digest_is_due,
    digest_week_key,
    parse_suspend_days,
    parse_suspend_reason,
    suspension_until,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import panel, send_card
from conductor.presentation.text import stamp
from conductor.presentation.tokens import Tone
from conductor.services.guild_config import get_or_create_config, remember_tz, tz_for
from conductor.services.runtime import log_tick
from conductor.services.staff_feed import emit_staff
from conductor.services.watchers import add_note, duty_ids, recent_notes, set_duty

log = structlog.get_logger("conductor.watchers_report")

SWEEP_SECONDS = 600
DIGEST_SECONDS = 300
BOT_LIFTED_BY = 0

__all__ = [
    "WatcherCardData",
    "WatchersReportService",
    "active_duty_ids",
    "active_suspension_ids",
    "count_active_suspensions",
    "load_active_suspension",
    "cached_active_suspension",
    "roster_metrics",
]


@dataclass(frozen=True, slots=True)
class WatcherCardData:
    metrics: WatcherMetrics
    notes: list[Any]
    actions: list[StaffActionView]


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _metrics() -> WatcherMetrics:
    return WatcherMetrics()


def _put(
    found: dict[int, WatcherMetrics],
    user_id: int,
    **changes: Any,
) -> None:
    current = found.get(int(user_id), _metrics())
    data = {field: getattr(current, field) for field in WatcherMetrics.__dataclass_fields__}
    data.update(changes)
    found[int(user_id)] = WatcherMetrics(**data)


async def load_active_suspension(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
) -> SuspensionView | None:
    moment = _aware(now or datetime.now(UTC))
    row = await session.scalar(
        select(WatcherSuspension)
        .where(
            WatcherSuspension.guild_id == int(guild_id),
            WatcherSuspension.user_id == int(user_id),
            WatcherSuspension.lifted_at.is_(None),
            WatcherSuspension.until > moment,
        )
        .order_by(WatcherSuspension.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return SuspensionView(
        user_id=int(row.user_id),
        set_by=int(row.set_by),
        reason=str(row.reason),
        until=row.until,
        lifted_at=row.lifted_at,
        lifted_by=None if row.lifted_by is None else int(row.lifted_by),
    )


async def load_active_suspensions(
    session: AsyncSession,
    guild_id: int,
    *,
    now: datetime | None = None,
) -> dict[int, SuspensionView]:
    moment = _aware(now or datetime.now(UTC))
    rows = (
        await session.scalars(
            select(WatcherSuspension)
            .where(
                WatcherSuspension.guild_id == int(guild_id),
                WatcherSuspension.lifted_at.is_(None),
                WatcherSuspension.until > moment,
            )
            .order_by(WatcherSuspension.created_at.desc())
        )
    ).all()
    found: dict[int, SuspensionView] = {}
    for row in rows:
        user_id = int(row.user_id)
        if user_id in found:
            continue
        found[user_id] = SuspensionView(
            user_id=user_id,
            set_by=int(row.set_by),
            reason=str(row.reason),
            until=row.until,
            lifted_at=row.lifted_at,
            lifted_by=None if row.lifted_by is None else int(row.lifted_by),
        )
    return found


async def cached_active_suspension(
    bot: Any,
    guild_id: int,
    user_id: int,
    *,
    now: datetime | None = None,
) -> SuspensionView | None:
    moment = _aware(now or datetime.now(UTC))
    gid = int(guild_id)
    uid = int(user_id)
    cache = getattr(getattr(bot, "caches", None), "suspensions", None)
    mapping = None if cache is None else cache.get(gid)
    if mapping is None:
        async with session_scope(bot.sessions) as session:
            mapping = await load_active_suspensions(session, gid, now=moment)
        if cache is not None:
            cache.set(gid, mapping)
    found = mapping.get(uid)
    if found is None:
        return None
    if found.lifted_at is not None or _aware(found.until) <= moment:
        return None
    return found


async def active_suspension_ids(
    session: AsyncSession,
    guild_id: int,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    moment = _aware(now or datetime.now(UTC))
    rows = (
        await session.scalars(
            select(WatcherSuspension.user_id).where(
                WatcherSuspension.guild_id == int(guild_id),
                WatcherSuspension.lifted_at.is_(None),
                WatcherSuspension.until > moment,
            )
        )
    ).all()
    return frozenset(int(user_id) for user_id in rows)


async def count_active_suspensions(
    session: AsyncSession,
    guild_id: int,
    *,
    now: datetime | None = None,
) -> int:
    return len(await active_suspension_ids(session, guild_id, now=now))


async def active_duty_ids(
    session: AsyncSession,
    guild_id: int,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    on_duty = await duty_ids(session, guild_id)
    blocked = await active_suspension_ids(session, guild_id, now=now)
    return frozenset(user_id for user_id in on_duty if user_id not in blocked)


async def _remind_hours(session: AsyncSession, guild_id: int) -> int:
    row = await session.get(ModSettings, int(guild_id))
    if row is None:
        return EVIDENCE_REMIND_HOURS_DEFAULT
    return int(row.evidence_remind_hours or EVIDENCE_REMIND_HOURS_DEFAULT)


async def roster_metrics(
    session: AsyncSession,
    guild_id: int,
    *,
    now: datetime | None = None,
) -> dict[int, WatcherMetrics]:
    moment = _aware(now or datetime.now(UTC))
    found: dict[int, WatcherMetrics] = {}
    week = moment - timedelta(days=7)
    month = moment - timedelta(days=30)
    hours = await _remind_hours(session, guild_id)
    escalate_before = moment - timedelta(hours=max(1, hours) * 3)

    case_rows = (
        await session.execute(
            select(ModCase.created_by, func.count())
            .where(
                ModCase.guild_id == int(guild_id),
                ModCase.created_at >= week,
                ModCase.revoked_at.is_(None),
            )
            .group_by(ModCase.created_by)
        )
    ).all()
    for user_id, total in case_rows:
        _put(found, int(user_id), cases_7d=int(total))

    revoked_rows = (
        await session.execute(
            select(ModCase.created_by, func.count())
            .where(
                ModCase.guild_id == int(guild_id),
                ModCase.revoked_at.is_not(None),
                ModCase.revoked_at >= month,
            )
            .group_by(ModCase.created_by)
        )
    ).all()
    for user_id, total in revoked_rows:
        _put(found, int(user_id), revoked_30d=int(total))

    overdue_rows = (
        await session.execute(
            select(ModCase.created_by, func.count())
            .select_from(ModEvidence)
            .join(ModCase, ModCase.id == ModEvidence.case_id)
            .where(
                ModEvidence.guild_id == int(guild_id),
                ModEvidence.channel_id.is_not(None),
                ModEvidence.sent_at.is_(None),
                ModEvidence.closed_by.is_(None),
                ModEvidence.created_at <= escalate_before,
            )
            .group_by(ModCase.created_by)
        )
    ).all()
    for user_id, total in overdue_rows:
        _put(found, int(user_id), overdue=int(total))

    ticket_rows = (
        await session.execute(
            select(Ticket.closed_by, func.count(), func.coalesce(func.sum(Ticket.rating), 0), func.count(Ticket.rating))
            .where(
                Ticket.guild_id == int(guild_id),
                Ticket.closed_by.is_not(None),
                Ticket.closed_at.is_not(None),
                Ticket.closed_at >= month,
            )
            .group_by(Ticket.closed_by)
        )
    ).all()
    for user_id, total, rating_sum, rating_n in ticket_rows:
        if user_id is None:
            continue
        _put(
            found,
            int(user_id),
            tickets_30d=int(total),
            rating_sum=int(rating_sum or 0),
            rating_n=int(rating_n or 0),
        )

    action_rows = (
        await session.execute(
            select(StaffAction.actor_id, func.max(StaffAction.created_at))
            .where(StaffAction.guild_id == int(guild_id))
            .group_by(StaffAction.actor_id)
        )
    ).all()
    for user_id, last_at in action_rows:
        _put(found, int(user_id), last_action=last_at)

    susp_rows = (
        await session.scalars(
            select(WatcherSuspension).where(
                WatcherSuspension.guild_id == int(guild_id),
                WatcherSuspension.lifted_at.is_(None),
                WatcherSuspension.until > moment,
            )
        )
    ).all()
    for row in susp_rows:
        _put(
            found,
            int(row.user_id),
            suspended_until=row.until,
            suspend_reason=str(row.reason),
        )
    return found


async def recent_actions(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    *,
    limit: int = ROSTER_FEED_LIMIT,
) -> list[StaffActionView]:
    rows = (
        await session.scalars(
            select(StaffAction)
            .where(StaffAction.guild_id == int(guild_id), StaffAction.actor_id == int(user_id))
            .order_by(StaffAction.created_at.desc())
            .limit(int(limit))
        )
    ).all()
    return [
        StaffActionView(
            action=str(row.action),
            created_at=row.created_at,
            details=row.details,
            target_id=None if row.target_id is None else int(row.target_id),
        )
        for row in rows
    ]


class WatchersReportService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(SWEEP_SECONDS, "suspension-sweep", self.sweep)
            every(DIGEST_SECONDS, "staff-digest", self.digest_tick)

    async def roster_metrics(self, guild_id: int, *, now: datetime | None = None) -> dict[int, WatcherMetrics]:
        async with session_scope(self._bot.sessions) as session:
            return await roster_metrics(session, guild_id, now=now)

    async def card_data(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> WatcherCardData:
        async with session_scope(self._bot.sessions) as session:
            metrics = (await roster_metrics(session, guild_id, now=now)).get(int(user_id), _metrics())
            notes = await recent_notes(session, guild_id, user_id, limit=5)
            actions = await recent_actions(session, guild_id, user_id)
        return WatcherCardData(metrics=metrics, notes=notes, actions=actions)

    async def is_suspended(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> bool:
        found = await cached_active_suspension(self._bot, guild_id, user_id, now=now)
        return found is not None

    async def count_active(self, guild_id: int, *, now: datetime | None = None) -> int:
        async with session_scope(self._bot.sessions) as session:
            return await count_active_suspensions(session, guild_id, now=now)

    async def guard_target(self, guild_id: int, user_id: int) -> GuardResource:
        from conductor.domain.access import Rank, resolve_rank
        from conductor.services.access import cached_snapshot

        snapshot = await cached_snapshot(self._bot, guild_id)
        guild = self._guild(guild_id)
        member = None if guild is None else guild.get_member(int(user_id))
        role_ids = tuple(role.id for role in getattr(member, "roles", ()) or ())
        rank = resolve_rank(role_ids, snapshot.rank_roles) if member is not None else Rank.NONE
        return GuardResource(
            ref=str(int(user_id)),
            guild_id=int(guild_id),
            target_id=int(user_id),
            target_rank=rank,
        )

    async def suspend(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int,
        actor_rank: Any,
        target_rank: Any,
        days_raw: str,
        reason: str,
        now: datetime | None = None,
    ) -> SuspensionView | str:
        days = parse_suspend_days(days_raw)
        cleaned = parse_suspend_reason(reason)
        if days is None:
            return "days"
        if cleaned is None:
            return "reason"
        if not can_suspend(actor=actor_rank, target=target_rank, actor_id=actor_id, target_id=user_id):
            return "hierarchy"
        moment = _aware(now or datetime.now(UTC))
        until = suspension_until(moment, days)
        async with session_scope(self._bot.sessions) as session:
            existing = await load_active_suspension(session, guild_id, user_id, now=moment)
            if existing is not None:
                return "already"
            session.add(
                WatcherSuspension(
                    guild_id=int(guild_id),
                    user_id=int(user_id),
                    set_by=int(actor_id),
                    reason=cleaned,
                    until=until,
                    created_at=moment,
                )
            )
            await set_duty(session, guild_id, user_id, False, actor_id=actor_id, now=moment)
        self._invalidate(guild_id, user_id)
        view = SuspensionView(user_id=int(user_id), set_by=int(actor_id), reason=cleaned, until=until)
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=actor_id,
            actor_rank=actor_rank,
            action=ACTION_WATCH_SUSPEND,
            target=user_id,
            details=copy.watchers_suspend_details(days=days, reason=cleaned),
        )
        await self._dm(
            user_id,
            panel(copy.WATCHERS_SUSPEND_DM_TITLE, copy.watchers_suspend_dm(stamp(until, "F"), cleaned), tone=Tone.WARN),
        )
        return view

    async def lift(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int,
        actor_rank: Any,
        target_rank: Any,
        now: datetime | None = None,
        expired: bool = False,
    ) -> SuspensionView | str:
        if not expired and not can_suspend(actor=actor_rank, target=target_rank, actor_id=actor_id, target_id=user_id):
            return "hierarchy"
        moment = _aware(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(WatcherSuspension)
                .where(
                    WatcherSuspension.guild_id == int(guild_id),
                    WatcherSuspension.user_id == int(user_id),
                    WatcherSuspension.lifted_at.is_(None),
                )
                .order_by(WatcherSuspension.created_at.desc())
                .limit(1)
            )
            if row is None:
                return "missing"
            row.lifted_at = moment
            row.lifted_by = int(actor_id)
            view = SuspensionView(
                user_id=int(row.user_id),
                set_by=int(row.set_by),
                reason=str(row.reason),
                until=row.until,
                lifted_at=moment,
                lifted_by=int(actor_id),
            )
        self._invalidate(guild_id, user_id)
        details = copy.WATCHERS_LIFT_EXPIRED if expired else copy.WATCHERS_LIFT_MANUAL
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=actor_id,
            actor_rank=actor_rank,
            action=ACTION_WATCH_LIFT,
            target=user_id,
            details=details,
        )
        await self._dm(user_id, panel(copy.WATCHERS_LIFT_DM_TITLE, copy.watchers_lift_dm(details), tone=Tone.OK))
        return view

    async def write_note(
        self,
        guild_id: int,
        user_id: int,
        *,
        author_id: int,
        actor_rank: Any,
        body: str,
        now: datetime | None = None,
    ) -> Any:
        moment = _aware(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            stored = await add_note(
                session,
                guild_id,
                user_id,
                author_id=author_id,
                body=body,
                now=moment,
            )
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=author_id,
            actor_rank=actor_rank,
            action=ACTION_WATCH_NOTE,
            target=user_id,
            details=stored.body,
        )
        return stored

    async def sweep(self, *, now: datetime | None = None) -> int:
        moment = _aware(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(WatcherSuspension).where(
                        WatcherSuspension.lifted_at.is_(None),
                        WatcherSuspension.until <= moment,
                    )
                )
            ).all()
            pending = [(int(row.guild_id), int(row.user_id)) for row in rows]
        from conductor.domain.access import Rank

        lifted = 0
        # Автоснятие подписывает бот, иначе лента показывает <@0>.
        bot_id = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0) or BOT_LIFTED_BY
        for guild_id, user_id in pending:
            result = await self.lift(
                guild_id,
                user_id,
                actor_id=bot_id,
                actor_rank=Rank.NONE,
                target_rank=Rank.NONE,
                now=moment,
                expired=True,
            )
            if isinstance(result, SuspensionView):
                lifted += 1
        log_tick(log, "suspension_sweep", changed=lifted)
        return lifted

    async def digest_tick(self, *, now: datetime | None = None) -> int:
        moment = _aware(now or datetime.now(UTC))
        posted = 0
        for guild in tuple(getattr(self._bot, "guilds", ()) or ()):
            guild_id = int(getattr(guild, "id", 0) or 0)
            if not guild_id:
                continue
            serves = getattr(self._bot, "serves", None)
            if callable(serves) and not serves(guild_id):
                continue
            if await self._digest_guild(guild_id, moment):
                posted += 1
        log_tick(log, "staff_digest", changed=posted)
        return posted

    async def _digest_guild(self, guild_id: int, now: datetime) -> bool:
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, guild_id)
            remember_tz(guild_id, config.timezone)
            local = now.astimezone(tz_for(guild_id))
            if not digest_is_due(local):
                return False
            week_key = digest_week_key(local)
            if config.staff_digest_week == week_key:
                return False
            body = await self._digest_body(session, guild_id, now)
            config.staff_digest_week = week_key
            channel_id = config.staff_feed_channel_id or config.admin_log_channel_id
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh):
            await refresh(guild_id)
        else:
            cached = getattr(self._bot, "config_by_guild", None)
            if cached is not None:
                row = cached.get(guild_id)
                if isinstance(row, GuildConfig):
                    row.staff_digest_week = week_key
        if channel_id is None:
            return False
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return False
        submit(
            int(channel_id),
            view=panel(copy.WATCHERS_DIGEST_TITLE, body, tone=Tone.INFO, footnote=copy.WATCHERS_DIGEST_FOOTNOTE),
        )
        return True

    async def _digest_body(self, session: AsyncSession, guild_id: int, now: datetime) -> str:
        week = now - timedelta(days=7)
        hours = await _remind_hours(session, guild_id)
        escalate_before = now - timedelta(hours=max(1, hours) * 3)
        cases = (
            await session.execute(
                select(ModCase.created_by, func.count())
                .where(
                    ModCase.guild_id == int(guild_id),
                    ModCase.created_at >= week,
                    ModCase.revoked_at.is_(None),
                )
                .group_by(ModCase.created_by)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()
        revoked_rows = (
            await session.execute(
                select(ModCase.created_by, func.count())
                .where(
                    ModCase.guild_id == int(guild_id),
                    ModCase.revoked_at.is_not(None),
                    ModCase.revoked_at >= week,
                )
                .group_by(ModCase.created_by)
            )
        ).all()
        overdue = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(ModEvidence)
                    .where(
                        ModEvidence.guild_id == int(guild_id),
                        ModEvidence.channel_id.is_not(None),
                        ModEvidence.sent_at.is_(None),
                        ModEvidence.closed_by.is_(None),
                        ModEvidence.created_at <= escalate_before,
                    )
                )
            )
            or 0
        )
        quota = int(
            (
                await session.scalar(
                    select(func.count()).where(
                        StaffAction.guild_id == int(guild_id),
                        StaffAction.action == ACTION_QUOTA_DENIED,
                        StaffAction.created_at >= week,
                    )
                )
            )
            or 0
        )
        closed_no_dm = int(
            (
                await session.scalar(
                    select(func.count()).where(
                        ModEvidence.guild_id == int(guild_id),
                        ModEvidence.closed_by.is_not(None),
                        ModEvidence.dm1_ok.is_(False),
                        ModEvidence.created_at >= week,
                    )
                )
            )
            or 0
        )
        opened = int(
            (
                await session.scalar(
                    select(func.count()).where(Ticket.guild_id == int(guild_id), Ticket.created_at >= week)
                )
            )
            or 0
        )
        closed = int(
            (
                await session.scalar(
                    select(func.count()).where(
                        Ticket.guild_id == int(guild_id),
                        Ticket.closed_at.is_not(None),
                        Ticket.closed_at >= week,
                    )
                )
            )
            or 0
        )
        rating_row = (
            await session.execute(
                select(func.avg(Ticket.rating), func.count(Ticket.rating)).where(
                    Ticket.guild_id == int(guild_id),
                    Ticket.closed_at.is_not(None),
                    Ticket.closed_at >= week,
                    Ticket.rating.is_not(None),
                )
            )
        ).one()
        rating_avg = None if rating_row[1] in (None, 0) else float(rating_row[0] or 0)
        unanswered = list(
            (
                await session.scalars(
                    select(Ticket.number)
                    .where(
                        Ticket.guild_id == int(guild_id),
                        Ticket.state.in_(tuple(state.value for state in WORKING_STATES)),
                        Ticket.claimed_by.is_(None),
                        Ticket.created_at <= now - UNANSWERED_AFTER,
                    )
                    .order_by(Ticket.number)
                )
            ).all()
        )
        pending = int(
            (
                await session.scalar(
                    select(func.count()).where(
                        Ticket.guild_id == int(guild_id),
                        Ticket.state == TicketState.PENDING_APPROVAL.value,
                    )
                )
            )
            or 0
        )
        appeal_base = (
            select(Ticket.id, Ticket.outcome, Ticket.created_at, Ticket.closed_at)
            .join(TicketCategory, Ticket.category_id == TicketCategory.id)
            .where(Ticket.guild_id == int(guild_id), TicketCategory.kind == CategoryKind.APPEAL.value)
        )
        appeal_rows = (await session.execute(appeal_base)).all()
        filed = sum(1 for row in appeal_rows if row.created_at >= week)
        granted_ids = [
            int(row.id)
            for row in appeal_rows
            if row.outcome == Outcome.ACCEPTED.value and row.closed_at is not None and row.closed_at >= week
        ]
        granted = len(granted_ids)
        bot_id = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0)
        automod_granted = 0
        if granted_ids and bot_id:
            automod_granted = int(
                (
                    await session.scalar(
                        select(func.count()).where(
                            ModCase.guild_id == int(guild_id),
                            ModCase.appeal_ticket_id.in_(granted_ids),
                            ModCase.created_by == bot_id,
                        )
                    )
                )
                or 0
            )
        funnel = None
        probe = getattr(getattr(self._bot, "verification", None), "funnel", None)
        if callable(probe):
            funnel = await probe(guild_id, 7, now=now)
        hit_rows = (
            await session.execute(
                select(AutomodHit.rule, func.count())
                .where(AutomodHit.guild_id == int(guild_id), AutomodHit.created_at >= week)
                .group_by(AutomodHit.rule)
                .order_by(func.count().desc())
            )
        ).all()
        channel_rows = (
            await session.execute(
                select(AutomodHit.channel_id, func.count())
                .where(AutomodHit.guild_id == int(guild_id), AutomodHit.created_at >= week)
                .group_by(AutomodHit.channel_id)
                .order_by(func.count().desc())
                .limit(3)
            )
        ).all()
        return copy.watchers_digest_body(
            top_cases=[(int(user_id), int(total)) for user_id, total in cases],
            revoked_total=sum(int(total) for _user_id, total in revoked_rows),
            revoked_who=[(int(user_id), int(total)) for user_id, total in revoked_rows],
            overdue=overdue,
            quota=quota,
            closed_no_dm=closed_no_dm,
            tickets_opened=opened,
            tickets_closed=closed,
            rating=rating_avg,
            unanswered=[int(number) for number in unanswered],
            pending=pending,
            appeals_filed=filed,
            appeals_granted=granted,
            automod_granted=automod_granted,
            joined=0 if funnel is None else int(funnel.joined),
            verified=0 if funnel is None else int(funnel.verified),
            trapped=0 if funnel is None else int(funnel.trapped),
            left=0 if funnel is None else int(funnel.left_within_24h),
            automod_rules=[(str(name), int(total)) for name, total in hit_rows],
            automod_channels=[(int(channel_id), int(total)) for channel_id, total in channel_rows],
        )

    def _invalidate(self, guild_id: int, user_id: int) -> None:
        caches = getattr(self._bot, "caches", None)
        drop = getattr(caches, "invalidate_member", None)
        if callable(drop):
            drop(int(guild_id), int(user_id))
        drop_sus = getattr(caches, "invalidate_suspensions", None)
        if callable(drop_sus):
            drop_sus(int(guild_id))

    def _guild(self, guild_id: int) -> Any:
        getter = getattr(self._bot, "get_guild", None)
        if callable(getter):
            return getter(int(guild_id))
        for guild in tuple(getattr(self._bot, "guilds", ()) or ()):
            if int(getattr(guild, "id", 0) or 0) == int(guild_id):
                return guild
        return None

    async def _dm(self, user_id: int, view: Any) -> bool:
        user = None
        getter = getattr(self._bot, "get_user", None)
        if callable(getter):
            user = getter(int(user_id))
        if user is None:
            fetch = getattr(self._bot, "fetch_user", None)
            if callable(fetch):
                try:
                    user = await self._discord(fetch(int(user_id)))
                except discord.HTTPException:
                    return False
        send = getattr(user, "send", None)
        if not callable(send):
            return False
        try:
            await self._discord(send_card(user, view))
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
