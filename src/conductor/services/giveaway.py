"""Розыгрыши: таймер из БД, счётчик пачками, приз-роль только через брокер."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    Giveaway,
    GiveawayEntry,
    GiveawaySettings,
    GiveawayWinner,
    MemberRecord,
    Reputation,
)
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.giveaway import (
    COUNTER_INTERVAL,
    MAX_TICKETS,
    MESSAGES_PER_TICKET,
    VOICE_MINUTES_PER_TICKET,
    Candidate,
    GiveawayCard,
    GiveawayRules,
    GiveawaySpec,
    GiveawayState,
    JoinOutcome,
    JoinVerdict,
    ManageVerdict,
    WinnerPlace,
    can_manage,
    can_reroll,
    dump_role_ids,
    dump_rules,
    evaluate,
    giveaway_state,
    parse_role_ids,
    parse_rules,
    pick_weighted,
    pick_winners,
    prize_role_allowed,
    ticket_weight,
    uses_ticket_weight,
)
from conductor.domain.reputation import is_frozen, tier_for
from conductor.domain.staff_feed import ACTION_GIVEAWAY_START
from conductor.presentation import copy
from conductor.presentation.giveaway import (
    PANEL_KIND,
    cancelled_view,
    finished_view,
    live_view,
    reveal_frames,
)
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import edit_card, send_card
from conductor.presentation.motion import MessageTarget, reveal
from conductor.services.old_roles import old_role_id_set
from conductor.services.panels import record_panel
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.runtime import guild_is_active, log_tick
from conductor.services.staff_feed import actor_rank_of, emit_staff

log = structlog.get_logger("conductor.giveaway")

TICK_PERIOD = 60.0


class GiveawayService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._rng = random.Random()
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(TICK_PERIOD, "giveaway-due", self.tick_due)

    async def bind_roles(self, guild_id: int) -> None:
        broker = getattr(self._bot, "roles", None)
        bind = getattr(broker, "bind", None)
        if not callable(bind):
            return
        allowed = await self.whitelist(guild_id)
        old = self._old_ids(guild_id)
        bind(guild_id, RoleSubsystem.GIVEAWAY, tuple(role_id for role_id in allowed if role_id not in old))

    async def whitelist(self, guild_id: int) -> tuple[int, ...]:
        async with session_scope(self._bot.sessions) as session:
            row = await self._settings(session, guild_id)
            return parse_role_ids(row.prize_role_ids)

    async def prize_role_ok(self, guild_id: int, role_id: int | None) -> bool:
        allowed = await self.whitelist(guild_id)
        return prize_role_allowed(role_id, whitelist=allowed, old_ids=self._old_ids(guild_id))

    async def allow_role(self, guild_id: int, role_id: int) -> bool:
        if role_id in self._old_ids(guild_id):
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await self._settings(session, guild_id)
            ids = set(parse_role_ids(row.prize_role_ids))
            ids.add(int(role_id))
            row.prize_role_ids = dump_role_ids(ids)
        await self.bind_roles(guild_id)
        return True

    async def deny_role(self, guild_id: int, role_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await self._settings(session, guild_id)
            ids = [item for item in parse_role_ids(row.prize_role_ids) if item != int(role_id)]
            row.prize_role_ids = dump_role_ids(ids)
        await self.bind_roles(guild_id)

    async def weight_formula(self, guild_id: int) -> tuple[int, int, int]:
        async with session_scope(self._bot.sessions) as session:
            row = await self._settings(session, guild_id)
            messages = max(1, int(row.weight_messages or MESSAGES_PER_TICKET))
            voice = max(1, int(row.weight_voice_minutes or VOICE_MINUTES_PER_TICKET))
            cap = max(1, min(MAX_TICKETS, int(row.weight_max or MAX_TICKETS)))
            return messages, voice, cap

    async def set_weight_formula(
        self,
        guild_id: int,
        *,
        messages: int | None = None,
        voice_minutes: int | None = None,
        cap: int | None = None,
    ) -> tuple[int, int, int] | None:
        current = await self.weight_formula(guild_id)
        next_messages = current[0] if messages is None else messages
        next_voice = current[1] if voice_minutes is None else voice_minutes
        next_cap = current[2] if cap is None else cap
        if next_messages < 1 or next_voice < 1 or next_cap < 1 or next_cap > MAX_TICKETS:
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await self._settings(session, guild_id)
            row.weight_messages = next_messages
            row.weight_voice_minutes = next_voice
            row.weight_max = next_cap
        return next_messages, next_voice, next_cap

    async def start(
        self,
        guild: discord.Guild,
        channel: Any,
        *,
        actor_id: int,
        spec: GiveawaySpec,
        prize_role_id: int | None = None,
        rules: GiveawayRules | None = None,
        now: datetime | None = None,
    ) -> int | None:
        send = getattr(channel, "send", None)
        if not callable(send):
            return None
        frozen = parse_rules(dump_rules(rules or GiveawayRules()))
        if not await self.prize_role_ok(guild.id, prize_role_id):
            return None
        if not await self.prize_role_ok(guild.id, frozen.consolation_role_id):
            return None
        moment = as_utc(now or datetime.now(UTC))
        ends_at = moment + spec.duration
        async with session_scope(self._bot.sessions) as session:
            row = Giveaway(
                guild_id=guild.id,
                channel_id=int(channel.id),
                message_id=None,
                owner_id=actor_id,
                prize=spec.prize,
                prize_role_id=prize_role_id,
                winners_count=spec.winners,
                ends_at=ends_at,
                created_at=moment,
                last_shown_count=0,
                rules_json=dump_rules(frozen),
            )
            session.add(row)
            await session.flush()
            giveaway_id = int(row.id)
        card = GiveawayCard(
            giveaway_id=giveaway_id,
            prize=spec.prize,
            host_id=actor_id,
            winners=spec.winners,
            ends_at=ends_at,
            entries=0,
            prize_role_id=prize_role_id,
            channel_id=int(channel.id),
            rules=frozen,
        )
        try:
            message = await self._discord(send_card(channel, live_view(card)))
        except discord.HTTPException as exc:
            log.warning("giveaway_send_failed", guild_id=guild.id, error=str(exc))
            async with session_scope(self._bot.sessions) as session:
                stale = await session.get(Giveaway, giveaway_id)
                if stale is not None:
                    await session.delete(stale)
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, giveaway_id)
            if row is None:
                return None
            row.message_id = int(message.id)
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=int(channel.id),
                message_id=int(message.id),
                ref=giveaway_id,
                created_by=actor_id,
                now=moment,
            )
        self._schedule_end(giveaway_id, max(0.0, (ends_at - moment).total_seconds()))
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor_id,
            actor_rank=await actor_rank_of(self._bot, guild, actor_id),
            action=ACTION_GIVEAWAY_START,
            target=int(channel.id),
            ref=giveaway_id,
            details=spec.prize,
            channel=True,
        )
        return giveaway_id

    async def join(
        self, guild: discord.Guild, giveaway_id: int, member: Any, *, now: datetime | None = None
    ) -> JoinOutcome:
        if bool(getattr(member, "bot", False)):
            return JoinOutcome(JoinVerdict.BOT)
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None or row.guild_id != guild.id:
                return JoinOutcome(JoinVerdict.MISSING)
            if giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at) is not GiveawayState.OPEN:
                return JoinOutcome(JoinVerdict.CLOSED)
            exists = await session.scalar(
                select(GiveawayEntry.id).where(
                    GiveawayEntry.giveaway_id == row.id,
                    GiveawayEntry.user_id == int(member.id),
                )
            )
            if exists is not None:
                return JoinOutcome(JoinVerdict.ALREADY)
            rules = parse_rules(row.rules_json)
            candidate = await self._candidate(session, guild, member)
        gaps = evaluate(candidate, rules, now=moment)
        if gaps:
            return JoinOutcome(JoinVerdict.UNFIT, gaps)
        try:
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(Giveaway, int(giveaway_id))
                if (
                    row is None
                    or giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at) is not GiveawayState.OPEN
                ):
                    return JoinOutcome(JoinVerdict.CLOSED)
                session.add(GiveawayEntry(giveaway_id=row.id, user_id=int(member.id), created_at=moment))
        except IntegrityError:
            return JoinOutcome(JoinVerdict.ALREADY)
        self._schedule_flush(int(giveaway_id))
        return JoinOutcome(JoinVerdict.JOINED)

    async def conclude(
        self, giveaway_id: int, *, actor_id: int | None = None, now: datetime | None = None
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None:
                return ManageVerdict.MISSING
            state = giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at)
            if state is GiveawayState.CANCELLED:
                return ManageVerdict.MISSING
            if state is GiveawayState.ENDED:
                return ManageVerdict.ALREADY
            rules = parse_rules(row.rules_json)
            stored = (
                int(row.id),
                int(row.guild_id),
                int(row.winners_count),
                rules,
            )
            pool = await self._entry_ids(session, row.id)
            snapshots = await self._records(session, stored[1], pool)
        guild = self._bot.get_guild(stored[1])
        if guild is None:
            return ManageVerdict.MISSING
        _giveaway_id, _guild_id, winners_count, rules = stored
        eligible = self._eligible(guild, pool, snapshots, rules, moment)
        if uses_ticket_weight(rules):
            picks = pick_weighted(eligible, winners_count, rng=self._rng)
        else:
            picks = pick_winners([user_id for user_id, _weight in eligible], winners_count, rng=self._rng)
        leftover = [user_id for user_id, _weight in eligible if user_id not in set(picks)]
        extras = pick_winners(leftover, rules.consolation_count, rng=self._rng) if rules.consolation_count else ()
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None:
                return ManageVerdict.MISSING
            state = giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at)
            if state is GiveawayState.CANCELLED:
                return ManageVerdict.MISSING
            if state is GiveawayState.ENDED:
                return ManageVerdict.ALREADY
            # Условный UPDATE: планировщик и кнопка «Завершить» могли дойти сюда одновременно —
            # победителей и выплату получает только тот, чей UPDATE реально закрыл розыгрыш.
            closed = await session.execute(
                update(Giveaway)
                .where(Giveaway.id == row.id, Giveaway.ended_at.is_(None), Giveaway.cancelled_at.is_(None))
                .values(ended_at=moment, closed_by=actor_id, last_shown_count=len(pool))
            )
            if int(closed.rowcount or 0) <= 0:
                return ManageVerdict.ALREADY
            await session.refresh(row)
            for user_id in picks:
                session.add(
                    GiveawayWinner(
                        giveaway_id=row.id,
                        user_id=user_id,
                        round=1,
                        place=WinnerPlace.MAIN,
                        created_at=moment,
                    )
                )
            for user_id in extras:
                session.add(
                    GiveawayWinner(
                        giveaway_id=row.id,
                        user_id=user_id,
                        round=1,
                        place=WinnerPlace.EXTRA,
                        created_at=moment,
                    )
                )
            card = self._card(
                row,
                entries=len(pool),
                winner_ids=picks,
                consolation_ids=extras,
                state=GiveawayState.ENDED,
            )
        self._cancel_jobs(int(giveaway_id))
        await self._grant(guild, card)
        await self._reveal(guild, card)
        return ManageVerdict.OK

    async def cancel(
        self,
        guild: discord.Guild,
        giveaway_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            state = giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at)
            if state is GiveawayState.CANCELLED:
                return ManageVerdict.ALREADY
            if state is GiveawayState.ENDED:
                return ManageVerdict.MISSING
            row.cancelled_at = moment
            row.closed_by = actor_id
            entries = await self._entry_count(session, row.id)
            card = self._card(row, entries=entries, state=GiveawayState.CANCELLED)
        self._cancel_jobs(int(giveaway_id))
        await self._edit(guild, card, cancelled_view(card))
        return ManageVerdict.OK

    async def reroll(
        self,
        guild: discord.Guild,
        giveaway_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            cancelled = row.cancelled_at is not None
            if not can_reroll(ended_at=row.ended_at, cancelled=cancelled, now=moment):
                if row.ended_at is not None and not cancelled:
                    return ManageVerdict.EXPIRED
                return ManageVerdict.MISSING
            previous = await self._winner_ids(session, row.id)
            pool = await self._entry_ids(session, row.id)
            rules = parse_rules(row.rules_json)
            snapshots = await self._records(session, row.guild_id, pool)
            winners_count = int(row.winners_count)
            highest = await session.scalar(
                select(func.max(GiveawayWinner.round)).where(GiveawayWinner.giveaway_id == row.id)
            )
            round_no = int(highest or 0) + 1
        eligible = self._eligible(guild, pool, snapshots, rules, moment)
        if uses_ticket_weight(rules):
            picks = pick_weighted(eligible, winners_count, exclude=previous, rng=self._rng)
        else:
            picks = pick_winners(
                [user_id for user_id, _weight in eligible],
                winners_count,
                exclude=previous,
                rng=self._rng,
            )
        if not picks:
            return ManageVerdict.EMPTY
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None:
                return ManageVerdict.MISSING
            for user_id in picks:
                session.add(
                    GiveawayWinner(
                        giveaway_id=row.id,
                        user_id=user_id,
                        round=round_no,
                        place=WinnerPlace.MAIN,
                        created_at=moment,
                    )
                )
            extras = await self._place_ids(session, row.id, WinnerPlace.EXTRA)
            entries = len(pool)
            card = self._card(
                row,
                entries=entries,
                winner_ids=picks,
                consolation_ids=extras,
                state=GiveawayState.ENDED,
            )
        await self._grant(guild, card)
        await self._edit(guild, card, finished_view(card))
        return ManageVerdict.OK

    async def flush_counter(self, giveaway_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, int(giveaway_id))
            if row is None:
                return
            if giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at) is not GiveawayState.OPEN:
                return
            entries = await self._entry_count(session, row.id)
            if entries == row.last_shown_count:
                return
            card = self._card(row, entries=entries)
            row.last_shown_count = entries
            guild_id = row.guild_id
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        await self._edit(guild, card, live_view(card))

    async def restore(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(Giveaway).where(Giveaway.ended_at.is_(None), Giveaway.cancelled_at.is_(None))
                )
            ).all()
            pending = [(int(row.id), int(row.guild_id), as_utc(row.ends_at)) for row in rows if row.message_id]
        for giveaway_id, guild_id, ends_at in pending:
            if not guild_is_active(self._bot, guild_id):
                continue
            self._schedule_end(giveaway_id, max(0.0, (ends_at - moment).total_seconds()))
        return len(pending)

    async def tick_due(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            due = (
                await session.scalars(
                    select(Giveaway.id).where(
                        Giveaway.ended_at.is_(None),
                        Giveaway.cancelled_at.is_(None),
                        Giveaway.message_id.is_not(None),
                        Giveaway.ends_at <= moment,
                    )
                )
            ).all()
        done = 0
        for giveaway_id in due:
            result = await self.conclude(int(giveaway_id), now=moment)
            if result is ManageVerdict.OK:
                done += 1
        log_tick(log, "giveaway_due", changed=done)
        return done

    async def open_in_channel(self, guild_id: int, channel_id: int) -> GiveawayCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Giveaway)
                .where(
                    Giveaway.guild_id == guild_id,
                    Giveaway.channel_id == channel_id,
                    Giveaway.ended_at.is_(None),
                    Giveaway.cancelled_at.is_(None),
                )
                .order_by(Giveaway.created_at.desc())
            )
            if row is None:
                return None
            entries = await self._entry_count(session, row.id)
            return self._card(row, entries=entries)

    async def ended_in_channel(self, guild_id: int, channel_id: int) -> GiveawayCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Giveaway)
                .where(
                    Giveaway.guild_id == guild_id,
                    Giveaway.channel_id == channel_id,
                    Giveaway.ended_at.is_not(None),
                    Giveaway.cancelled_at.is_(None),
                )
                .order_by(Giveaway.ended_at.desc())
            )
            if row is None:
                return None
            entries = await self._entry_count(session, row.id)
            winners = await self._place_ids(session, row.id, WinnerPlace.MAIN, latest=True)
            extras = await self._place_ids(session, row.id, WinnerPlace.EXTRA)
            return self._card(
                row,
                entries=entries,
                winner_ids=winners,
                consolation_ids=extras,
                state=GiveawayState.ENDED,
            )

    async def guard_resource(self, guild_id: int, ref: int | str, *, need: str = "open") -> GuardResource | None:
        try:
            giveaway_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Giveaway, giveaway_id)
            if row is None or row.guild_id != guild_id:
                return None
            state = giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at)
            closed = True
            if need == "open":
                closed = state is not GiveawayState.OPEN
            elif need == "ended":
                closed = state is not GiveawayState.ENDED or not can_reroll(
                    ended_at=row.ended_at,
                    cancelled=row.cancelled_at is not None,
                    now=datetime.now(UTC),
                )
            return GuardResource(
                ref=str(row.id),
                guild_id=row.guild_id,
                owner_id=row.owner_id,
                closed=closed,
                channel_id=row.channel_id,
            )

    def _schedule_end(self, giveaway_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("gw-end", giveaway_id), delay, lambda gid=giveaway_id: self.conclude(gid))

    def _schedule_flush(self, giveaway_id: int) -> None:
        cards = getattr(self._bot, "cards", None)
        request = getattr(cards, "request", None)
        if callable(request):
            request(("giveaway", int(giveaway_id)), lambda: self.flush_counter(giveaway_id), window=COUNTER_INTERVAL)
            return
        queue = getattr(self._bot, "queue", None)
        until = getattr(queue, "until", None)
        touch = getattr(queue, "touch", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(until) or not callable(touch) or not callable(schedule):
            return
        if until(("gw-count", giveaway_id)) > 0:
            return
        touch(("gw-count", giveaway_id), COUNTER_INTERVAL)
        schedule(("gw-flush", giveaway_id), COUNTER_INTERVAL, lambda gid=giveaway_id: self.flush_counter(gid))

    def _cancel_jobs(self, giveaway_id: int) -> None:
        cards = getattr(self._bot, "cards", None)
        drop = getattr(cards, "cancel", None)
        if callable(drop):
            drop(("giveaway", int(giveaway_id)))
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if not callable(cancel):
            return
        cancel(("gw-end", giveaway_id))
        cancel(("gw-flush", giveaway_id))

    async def _grant(self, guild: discord.Guild, card: GiveawayCard) -> None:
        whitelist = await self.whitelist(guild.id)
        old = self._old_ids(guild.id)
        broker = getattr(self._bot, "roles", None)
        apply = getattr(broker, "apply", None)
        if not callable(apply):
            return
        prize_id = card.prize_role_id
        prize_ok = prize_id is not None and card.winner_ids
        if prize_ok and prize_role_allowed(prize_id, whitelist=whitelist, old_ids=old):
            for user_id in card.winner_ids:
                await apply(
                    RoleClaim(
                        guild_id=guild.id,
                        user_id=user_id,
                        subsystem=RoleSubsystem.GIVEAWAY,
                        add=(prize_id,),
                        reason=copy.REASON_GIVEAWAY_PRIZE,
                        urgent=True,
                    )
                )
        extra_id = card.rules.consolation_role_id
        extra_ok = extra_id is not None and card.consolation_ids
        if extra_ok and prize_role_allowed(extra_id, whitelist=whitelist, old_ids=old):
            for user_id in card.consolation_ids:
                await apply(
                    RoleClaim(
                        guild_id=guild.id,
                        user_id=user_id,
                        subsystem=RoleSubsystem.GIVEAWAY,
                        add=(extra_id,),
                        reason=copy.REASON_GIVEAWAY_CONSOLATION,
                        urgent=True,
                    )
                )

    async def _reveal(self, guild: discord.Guild, card: GiveawayCard) -> None:
        message = self._message(guild, card)
        if message is None:
            return
        registry = getattr(self._bot, "motion", None)
        frames = reveal_frames(card, registry=registry if hasattr(registry, "render") else None)
        pause = getattr(self._bot, "sleep", None)
        await reveal(MessageTarget(message), frames, sleep=pause if callable(pause) else None)

    async def _edit(self, guild: discord.Guild, card: GiveawayCard, view: Any) -> None:
        message = self._message(guild, card)
        if message is None:
            return
        try:
            await self._discord(edit_card(message, view))
        except discord.HTTPException as exc:
            log.warning("giveaway_edit_failed", giveaway_id=card.giveaway_id, error=str(exc))

    def _message(self, guild: discord.Guild, card: GiveawayCard) -> Any | None:
        return self._locate(guild, card.channel_id, card.message_id)

    def _locate(self, guild: discord.Guild, channel_id: int, message_id: int | None) -> Any | None:
        if message_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            getter = getattr(guild, "get_thread", None)
            channel = getter(channel_id) if callable(getter) else None
        if channel is None:
            return None
        partial = getattr(channel, "get_partial_message", None)
        if callable(partial):
            return partial(message_id)
        messages = getattr(channel, "messages", None)
        if isinstance(messages, dict):
            return messages.get(message_id)
        return None

    def _old_ids(self, guild_id: int) -> set[int]:
        configs = getattr(self._bot, "config_by_guild", None) or {}
        config = configs.get(guild_id) if hasattr(configs, "get") else None
        if config is None:
            return set()
        try:
            return old_role_id_set(config)
        except AttributeError:
            return set()

    async def _settings(self, session: AsyncSession, guild_id: int) -> GiveawaySettings:
        row = await session.get(GiveawaySettings, guild_id)
        if row is None:
            row = GiveawaySettings(guild_id=guild_id, prize_role_ids="[]")
            session.add(row)
            await session.flush()
        return row

    async def _entry_ids(self, session: AsyncSession, giveaway_id: int) -> tuple[int, ...]:
        rows = (
            await session.scalars(select(GiveawayEntry.user_id).where(GiveawayEntry.giveaway_id == giveaway_id))
        ).all()
        return tuple(int(user_id) for user_id in rows)

    async def _entry_count(self, session: AsyncSession, giveaway_id: int) -> int:
        value = await session.scalar(
            select(func.count()).select_from(GiveawayEntry).where(GiveawayEntry.giveaway_id == giveaway_id)
        )
        return int(value or 0)

    async def _winner_ids(self, session: AsyncSession, giveaway_id: int) -> tuple[int, ...]:
        rows = (
            await session.scalars(select(GiveawayWinner.user_id).where(GiveawayWinner.giveaway_id == giveaway_id))
        ).all()
        return tuple(int(user_id) for user_id in rows)

    async def _place_ids(
        self,
        session: AsyncSession,
        giveaway_id: int,
        place: WinnerPlace | str,
        *,
        latest: bool = False,
    ) -> tuple[int, ...]:
        query = select(GiveawayWinner).where(
            GiveawayWinner.giveaway_id == giveaway_id,
            GiveawayWinner.place == str(place),
        )
        if latest:
            highest = await session.scalar(
                select(func.max(GiveawayWinner.round)).where(
                    GiveawayWinner.giveaway_id == giveaway_id,
                    GiveawayWinner.place == str(place),
                )
            )
            if highest is None:
                return ()
            query = query.where(GiveawayWinner.round == int(highest))
        rows = (await session.scalars(query)).all()
        return tuple(int(row.user_id) for row in rows)

    async def _candidate(self, session: AsyncSession, guild: discord.Guild, member: Any) -> Candidate:
        snapshots = await self._records(session, guild.id, (int(member.id),))
        built = self._from_record(guild, int(member.id), snapshots.get(int(member.id)))
        roles = frozenset(int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None))
        voice = getattr(getattr(member, "voice", None), "channel", None) is not None
        created = built.account_created_at or getattr(member, "created_at", None)
        joined = built.joined_at or getattr(member, "joined_at", None)
        return Candidate(
            user_id=int(member.id),
            role_ids=roles,
            messages=built.messages,
            voice_seconds=built.voice_seconds,
            account_created_at=created,
            joined_at=joined,
            sinner=built.sinner,
            in_voice=voice,
        )

    async def _records(
        self, session: AsyncSession, guild_id: int, user_ids: Sequence[int]
    ) -> dict[int, tuple[MemberRecord | None, int | None]]:
        ids = tuple(int(user_id) for user_id in user_ids)
        if not ids:
            return {}
        members = (
            await session.scalars(
                select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id.in_(ids))
            )
        ).all()
        scores = (
            await session.scalars(
                select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id.in_(ids))
            )
        ).all()
        by_member = {int(row.user_id): row for row in members}
        by_score = {int(row.user_id): int(row.score) for row in scores}
        return {user_id: (by_member.get(user_id), by_score.get(user_id)) for user_id in ids}

    def _from_record(
        self,
        guild: discord.Guild,
        user_id: int,
        snapshot: tuple[MemberRecord | None, int | None] | None,
    ) -> Candidate:
        record, score = snapshot if snapshot is not None else (None, None)
        member = guild.get_member(user_id)
        roles = frozenset(int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None))
        voice = getattr(getattr(member, "voice", None), "channel", None) is not None
        sinner = False if score is None else is_frozen(tier_for(score))
        created = None if record is None else record.account_created_at
        joined = None if record is None else record.first_joined_at
        if created is None:
            created = getattr(member, "created_at", None)
        if joined is None:
            joined = getattr(member, "joined_at", None)
        return Candidate(
            user_id=user_id,
            role_ids=roles,
            messages=0 if record is None else int(record.message_count or 0),
            voice_seconds=0 if record is None else int(record.voice_seconds or 0),
            account_created_at=created,
            joined_at=joined,
            sinner=sinner,
            in_voice=voice,
        )

    def _eligible(
        self,
        guild: discord.Guild,
        user_ids: Sequence[int],
        snapshots: dict[int, tuple[MemberRecord | None, int | None]],
        rules: GiveawayRules,
        now: datetime,
    ) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for user_id in user_ids:
            candidate = self._from_record(guild, int(user_id), snapshots.get(int(user_id)))
            if evaluate(candidate, rules, now=now):
                continue
            found.append((int(user_id), ticket_weight(candidate, rules)))
        return found

    def _card(
        self,
        row: Giveaway,
        *,
        entries: int,
        winner_ids: tuple[int, ...] = (),
        consolation_ids: tuple[int, ...] = (),
        state: GiveawayState | None = None,
    ) -> GiveawayCard:
        return GiveawayCard(
            giveaway_id=int(row.id),
            prize=row.prize,
            host_id=int(row.owner_id),
            winners=int(row.winners_count),
            ends_at=as_utc(row.ends_at),
            entries=entries,
            state=state or giveaway_state(ended_at=row.ended_at, cancelled_at=row.cancelled_at),
            winner_ids=winner_ids,
            prize_role_id=None if row.prize_role_id is None else int(row.prize_role_id),
            ended_at=None if row.ended_at is None else as_utc(row.ended_at),
            channel_id=int(row.channel_id),
            message_id=None if row.message_id is None else int(row.message_id),
            rules=parse_rules(row.rules_json),
            consolation_ids=consolation_ids,
        )

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
