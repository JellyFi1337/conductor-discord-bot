"""Лестница активности: счётчики, сверка, роли только через брокер."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    LadderProgress,
    LadderSettings,
    MemberDaily,
    MemberRecord,
    Reputation,
    VoiceSession,
)
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.ladder import (
    DEFAULT_CONFIG,
    STEPS,
    LadderCard,
    LadderConfig,
    LadderPanel,
    LadderRoles,
    LadderStats,
    Step,
    desired_step,
    earned_step,
    held_from_roles,
    max_step,
    next_need,
    parse_ladder_config,
    parse_step,
    role_plan,
    settings_need_defaults,
    tenure_days,
)
from conductor.domain.reputation import ScoreChanged, is_frozen, tier_for
from conductor.domain.voice import session_seconds
from conductor.presentation import copy
from conductor.services.queue import UniqueQueue
from conductor.services.role_broker import RoleClaim, RoleSubsystem, log_role_failure, report_skipped_roles
from conductor.services.runtime import guild_is_active, log_tick, member_on_guild

log = structlog.get_logger("conductor.ladder")

PERIOD = 600.0
COOLDOWN = 600.0


class LadderService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._jobs: UniqueQueue[tuple[int, int]] = UniqueQueue()
        events = getattr(bot, "events", None)
        subscribe = getattr(events, "subscribe", None)
        if callable(subscribe):
            subscribe(ScoreChanged, self._on_score)
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(PERIOD, "ladder-reconcile", self.tick_all)
        self._skipped_alerts: set[int] = set()

    def _on_score(self, event: ScoreChanged) -> None:
        self.enqueue(event.guild_id, event.user_id)

    def enqueue(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        queue = getattr(self._bot, "queue", None)
        if queue is None:
            self._jobs.put(key, key)
            return
        delay = queue.until(("ladder", key[0], key[1]))
        queue.schedule_later(("ladder-run", key[0], key[1]), delay, lambda: self.reconcile(key[0], key[1]))

    async def restore(self) -> int:
        async with session_scope(self._bot.sessions) as session:
            daily = (await session.scalars(select(MemberDaily.guild_id).distinct())).all()
            progress = (await session.scalars(select(LadderProgress.guild_id).distinct())).all()
            voice = (await session.scalars(select(VoiceSession.guild_id).distinct())).all()
            guild_ids = {int(guild_id) for guild_id in (*daily, *progress, *voice)}
        scheduled = 0
        for guild_id in sorted(guild_ids):
            if not guild_is_active(self._bot, guild_id):
                continue
            for user_id in await self._candidates(guild_id):
                self.enqueue(guild_id, user_id)
                scheduled += 1
        return scheduled

    async def tick_all(self) -> None:
        now = datetime.now(UTC)
        scheduled = 0
        for guild in getattr(self._bot, "guilds", ()):
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            try:
                for user_id in await self._candidates(int(guild_id)):
                    if not member_on_guild(self._bot, int(guild_id), int(user_id)):
                        continue
                    self.enqueue(int(guild_id), int(user_id))
                    scheduled += 1
            except Exception:
                log.exception("ladder_tick_failed", guild_id=guild_id)
        if getattr(self._bot, "queue", None) is None:
            await self.drain(now=now)
        log_tick(log, "ladder_reconcile_tick", changed=0, scheduled=scheduled)

    async def drain(self, *, now: datetime | None = None) -> int:
        done = 0
        while True:
            item = self._jobs.pop()
            if item is None:
                break
            _key, payload = item
            guild_id, user_id = payload
            await self.reconcile(guild_id, user_id, now=now)
            done += 1
        return done

    async def card(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> LadderCard:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            config, roles = await self._load(session, guild_id)
            stats = await self._stats(session, guild_id, user_id, moment)
            joined_at = await self._joined_at(session, guild_id, user_id)
            held = await self._held(session, guild_id, user_id)
            sinner = await self._sinner(session, guild_id, user_id)
        earned = earned_step(stats, config, now=moment, joined_at=joined_at)
        desired = desired_step(earned=earned, held=held, sinner=sinner)
        return LadderCard(
            user_id=user_id,
            held=held,
            earned=earned,
            desired=desired,
            sinner=sinner,
            stats=stats,
            need=next_need(held, stats, config, now=moment, joined_at=joined_at, sinner=sinner),
            tenure_days=tenure_days(joined_at, moment),
        )

    async def show_config(self, guild_id: int) -> LadderPanel:
        async with session_scope(self._bot.sessions) as session:
            config, roles = await self._load(session, guild_id)
            return LadderPanel(config=config, roles=roles)

    async def update_config(
        self,
        guild_id: int,
        *,
        guest_role_id: int | None = None,
        regular_role_id: int | None = None,
        resident_role_id: int | None = None,
        friend_role_id: int | None = None,
        regular_messages: int | None = None,
        regular_voice_minutes: int | None = None,
        regular_reactions: int | None = None,
        resident_messages: int | None = None,
        resident_voice_hours: int | None = None,
        resident_days: int | None = None,
        resident_strong_messages: int | None = None,
        resident_strong_voice_hours: int | None = None,
        friend_days: int | None = None,
        friend_messages: int | None = None,
        friend_voice_hours: int | None = None,
        friend_reactions: int | None = None,
    ) -> LadderPanel | None:
        async with session_scope(self._bot.sessions) as session:
            current, roles = await self._load(session, guild_id)
            parsed = parse_ladder_config(
                current,
                regular_messages=regular_messages,
                regular_voice_minutes=regular_voice_minutes,
                regular_reactions=regular_reactions,
                resident_messages=resident_messages,
                resident_voice_hours=resident_voice_hours,
                resident_days=resident_days,
                resident_strong_messages=resident_strong_messages,
                resident_strong_voice_hours=resident_strong_voice_hours,
                friend_days=friend_days,
                friend_messages=friend_messages,
                friend_voice_hours=friend_voice_hours,
                friend_reactions=friend_reactions,
            )
            if parsed is None:
                return None
            row = await session.get(LadderSettings, guild_id)
            if row is None:
                row = LadderSettings(guild_id=guild_id)
                session.add(row)
            self._write_settings(row, parsed)
            if guest_role_id is not None:
                row.guest_role_id = guest_role_id
            if regular_role_id is not None:
                row.regular_role_id = regular_role_id
            if resident_role_id is not None:
                row.resident_role_id = resident_role_id
            if friend_role_id is not None:
                row.friend_role_id = friend_role_id
            roles = LadderRoles(
                guest=row.guest_role_id,
                regular=row.regular_role_id,
                resident=row.resident_role_id,
                friend=row.friend_role_id,
            )
            panel = LadderPanel(config=parsed, roles=roles)
        self._skipped_alerts.discard(int(guild_id))
        self._bind(guild_id, roles)
        return panel

    async def defaults_drift(self, guild_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LadderSettings, guild_id)
            if row is None:
                return False
            config, _roles = await self._load(session, guild_id)
            return settings_need_defaults(config)

    async def apply_new_defaults(self, guild_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LadderSettings, guild_id)
            if row is None:
                return False
            self._write_settings(row, DEFAULT_CONFIG)
            return True

    async def bind_roles(self, guild_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            _config, roles = await self._load(session, guild_id)
        self._skipped_alerts.discard(int(guild_id))
        self._bind(guild_id, roles)

    async def bound_roles(self, guild_id: int) -> tuple[tuple[str, int], ...]:
        async with session_scope(self._bot.sessions) as session:
            _config, roles = await self._load(session, guild_id)
        found: list[tuple[str, int]] = []
        for step in STEPS:
            role_id = roles.for_step(step)
            if role_id:
                found.append((step.key, role_id))
        return tuple(found)

    async def grant_held(self, guild_id: int, user_id: int, step: Step, *, now: datetime | None = None) -> Step:
        """Записать ступень и выдать роль. Понижения нет: сверка оставит максимум."""
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            _config, roles = await self._load(session, guild_id)
            await self._store_step(session, int(guild_id), int(user_id), step, moment)
        have = self._member_role_ids(int(guild_id), int(user_id))
        add, remove = role_plan(have=have, desired=step, roles=roles)
        self._bind(int(guild_id), roles)
        if add or remove:
            await self._apply(int(guild_id), int(user_id), add=add, remove=remove, desired=step)
        return step

    async def reconcile(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> tuple[int, ...] | None:
        moment = as_utc(now or datetime.now(UTC))
        queue = getattr(self._bot, "queue", None)
        if queue is not None:
            queue.touch(("ladder", guild_id, user_id), COOLDOWN)
        async with session_scope(self._bot.sessions) as session:
            config, roles = await self._load(session, guild_id)
            stats = await self._stats(session, guild_id, user_id, moment)
            joined_at = await self._joined_at(session, guild_id, user_id)
            stored = await self._held(session, guild_id, user_id)
            sinner = await self._sinner(session, guild_id, user_id)
        have = self._member_role_ids(guild_id, user_id)
        discord_held = held_from_roles(have, roles)
        held = max_step(stored, discord_held)
        earned = earned_step(stats, config, now=moment, joined_at=joined_at)
        desired = desired_step(earned=earned, held=held, sinner=sinner)
        add, remove = role_plan(have=have, desired=desired, roles=roles)
        async with session_scope(self._bot.sessions) as session:
            await self._store_step(session, guild_id, user_id, desired, moment)
        self._bind(guild_id, roles)
        if not add and not remove:
            return ()
        if not member_on_guild(self._bot, guild_id, user_id):
            return ()
        result = await self._apply(guild_id, user_id, add=add, remove=remove, desired=desired)
        if result is None:
            return None
        return add + remove

    async def _apply(
        self,
        guild_id: int,
        user_id: int,
        *,
        add: tuple[int, ...],
        remove: tuple[int, ...],
        desired: Step,
    ) -> object | None:
        broker = getattr(self._bot, "roles", None)
        apply = getattr(broker, "apply", None)
        if not callable(apply):
            return None
        claim = RoleClaim(
            guild_id=guild_id,
            user_id=user_id,
            subsystem=RoleSubsystem.ACTIVITY,
            add=add,
            remove=remove,
            reason=copy.reason_ladder(desired.key),
        )
        result = await apply(claim)
        skipped = tuple(getattr(result, "skipped", ()) or ())
        if skipped:
            report_skipped_roles(
                self._bot,
                guild_id,
                skipped,
                seen=self._skipped_alerts,
                title=copy.LADDER_ROLE_GONE,
                hint=copy.LADDER_STALE_ACTION,
            )
        elif not getattr(result, "ok", True):
            log_role_failure(
                log,
                "ladder_role_failed",
                guild_id=guild_id,
                user_id=user_id,
                result=result,
            )
        if getattr(result, "called", False) is False and not add and not remove:
            return result
        return result

    async def _candidates(self, guild_id: int) -> list[int]:
        async with session_scope(self._bot.sessions) as session:
            daily = set(
                (
                    await session.scalars(
                        select(MemberDaily.user_id).where(MemberDaily.guild_id == guild_id).distinct()
                    )
                ).all()
            )
            rows = set(
                (await session.scalars(select(LadderProgress.user_id).where(LadderProgress.guild_id == guild_id))).all()
            )
            voice = set(
                (
                    await session.scalars(
                        select(VoiceSession.user_id).where(VoiceSession.guild_id == guild_id).distinct()
                    )
                ).all()
            )
        return sorted({int(user_id) for user_id in daily | rows | voice})

    async def _stats(self, session: AsyncSession, guild_id: int, user_id: int, now: datetime) -> LadderStats:
        messages = int(
            await session.scalar(
                select(func.coalesce(func.sum(MemberDaily.messages), 0)).where(
                    MemberDaily.guild_id == guild_id,
                    MemberDaily.user_id == user_id,
                )
            )
            or 0
        )
        reactions = int(
            await session.scalar(
                select(func.coalesce(func.sum(MemberDaily.reactions), 0)).where(
                    MemberDaily.guild_id == guild_id,
                    MemberDaily.user_id == user_id,
                )
            )
            or 0
        )
        closed = int(
            await session.scalar(
                select(func.coalesce(func.sum(VoiceSession.duration_seconds), 0)).where(
                    VoiceSession.guild_id == guild_id,
                    VoiceSession.user_id == user_id,
                    VoiceSession.left_at.is_not(None),
                )
            )
            or 0
        )
        open_rows = (
            await session.scalars(
                select(VoiceSession).where(
                    VoiceSession.guild_id == guild_id,
                    VoiceSession.user_id == user_id,
                    VoiceSession.left_at.is_(None),
                )
            )
        ).all()
        live = sum(session_seconds(row.joined_at, now) for row in open_rows)
        return LadderStats(messages=messages, reactions=reactions, voice_seconds=closed + live)

    async def _joined_at(self, session: AsyncSession, guild_id: int, user_id: int) -> datetime | None:
        row = await session.scalar(
            select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
        )
        if row is None:
            return None
        return row.first_joined_at

    async def _held(self, session: AsyncSession, guild_id: int, user_id: int) -> Step:
        row = await session.scalar(
            select(LadderProgress).where(LadderProgress.guild_id == guild_id, LadderProgress.user_id == user_id)
        )
        if row is None:
            return Step.GUEST
        return parse_step(row.step)

    async def _sinner(self, session: AsyncSession, guild_id: int, user_id: int) -> bool:
        row = await session.scalar(
            select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
        )
        if row is None:
            return False
        return is_frozen(tier_for(int(row.score)))

    async def _store_step(
        self,
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        step: Step,
        now: datetime,
    ) -> None:
        row = await session.scalar(
            select(LadderProgress).where(LadderProgress.guild_id == guild_id, LadderProgress.user_id == user_id)
        )
        if row is None:
            session.add(
                LadderProgress(
                    guild_id=guild_id,
                    user_id=user_id,
                    step=step.key,
                    updated_at=now,
                )
            )
            return
        row.step = step.key
        row.updated_at = now

    async def _load(self, session: AsyncSession, guild_id: int) -> tuple[LadderConfig, LadderRoles]:
        row = await session.get(LadderSettings, guild_id)
        if row is None:
            return DEFAULT_CONFIG, LadderRoles()
        config = LadderConfig(
            regular_messages=row.regular_messages,
            regular_voice_seconds=row.regular_voice_seconds,
            regular_reactions=row.regular_reactions,
            resident_messages=row.resident_messages,
            resident_voice_seconds=row.resident_voice_seconds,
            resident_days=int(row.resident_days),
            resident_strong_messages=row.resident_strong_messages,
            resident_strong_voice_seconds=row.resident_strong_voice_seconds,
            friend_tenure_days=row.friend_tenure_days,
            friend_messages=row.friend_messages,
            friend_voice_seconds=row.friend_voice_seconds,
            friend_reactions=row.friend_reactions,
        )
        roles = LadderRoles(
            guest=row.guest_role_id,
            regular=row.regular_role_id,
            resident=row.resident_role_id,
            friend=row.friend_role_id,
        )
        return config, roles

    def _write_settings(self, row: LadderSettings, config: LadderConfig) -> None:
        row.regular_messages = config.regular_messages
        row.regular_voice_seconds = config.regular_voice_seconds
        row.regular_reactions = config.regular_reactions
        row.resident_messages = config.resident_messages
        row.resident_voice_seconds = config.resident_voice_seconds
        row.resident_days = config.resident_days
        row.resident_strong_messages = config.resident_strong_messages
        row.resident_strong_voice_seconds = config.resident_strong_voice_seconds
        row.friend_tenure_days = config.friend_tenure_days
        row.friend_messages = config.friend_messages
        row.friend_voice_seconds = config.friend_voice_seconds
        row.friend_reactions = config.friend_reactions

    def _bind(self, guild_id: int, roles: LadderRoles) -> None:
        broker = getattr(self._bot, "roles", None)
        bind = getattr(broker, "bind", None)
        if not callable(bind):
            return
        bind(guild_id, RoleSubsystem.ACTIVITY, roles.bound())

    def _member_role_ids(self, guild_id: int, user_id: int) -> tuple[int, ...]:
        getter = getattr(self._bot, "get_guild", None)
        guild = getter(guild_id) if callable(getter) else None
        if guild is None:
            return ()
        member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
        if member is None:
            return ()
        return tuple(int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None))
