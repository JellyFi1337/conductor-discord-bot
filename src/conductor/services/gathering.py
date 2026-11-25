"""Сборы: места, лист ожидания, пинг идущих, комната на организатора."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import Gathering, GatheringRsvp
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.gathering import (
    FLUSH_INTERVAL,
    GatheringCard,
    GatheringSpec,
    GatheringState,
    ManageVerdict,
    Rsvp,
    RsvpStatus,
    RsvpVerdict,
    apply_checkin,
    apply_rsvp,
    can_manage,
    gathering_state,
    ping_ready,
)
from conductor.domain.rooms import created_channel_id
from conductor.presentation.gathering import PANEL_KIND, cancelled_view, live_view, ping_view
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import edit_card, send_card
from conductor.services.broadcast import ping_many
from conductor.services.panels import record_panel
from conductor.services.pulse import bump_pulse
from conductor.services.runtime import guild_is_active

log = structlog.get_logger("conductor.gathering")


class GatheringService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def start(
        self,
        guild: discord.Guild,
        channel: Any,
        *,
        actor_id: int,
        spec: GatheringSpec,
        now: datetime | None = None,
    ) -> int | None:
        send = getattr(channel, "send", None)
        if not callable(send):
            return None
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = Gathering(
                guild_id=guild.id,
                channel_id=int(channel.id),
                message_id=None,
                owner_id=actor_id,
                title=spec.title,
                starts_at=spec.starts_at,
                seats=spec.seats,
                created_at=moment,
            )
            session.add(row)
            await session.flush()
            gathering_id = int(row.id)
        card = GatheringCard(
            gathering_id=gathering_id,
            title=spec.title,
            host_id=actor_id,
            starts_at=spec.starts_at,
            seats=spec.seats,
            channel_id=int(channel.id),
        )
        try:
            message = await self._discord(
                send_card(channel, live_view(card, now=moment))
            )
        except discord.HTTPException as exc:
            log.warning("gathering_send_failed", guild_id=guild.id, error=str(exc))
            async with session_scope(self._bot.sessions) as session:
                stale = await session.get(Gathering, gathering_id)
                if stale is not None:
                    await session.delete(stale)
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, gathering_id)
            if row is None:
                return None
            row.message_id = int(message.id)
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=int(channel.id),
                message_id=int(message.id),
                ref=gathering_id,
                created_by=actor_id,
                now=moment,
            )
        self._schedule_start(gathering_id, max(0.0, (as_utc(spec.starts_at) - moment).total_seconds()))
        bump_pulse(self._bot, guild.id, now=moment)
        return gathering_id

    async def rsvp(
        self,
        guild: discord.Guild,
        gathering_id: int,
        member: Any,
        status: RsvpStatus,
        *,
        now: datetime | None = None,
    ) -> RsvpVerdict:
        if bool(getattr(member, "bot", False)):
            return RsvpVerdict.BOT
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild.id:
                return RsvpVerdict.MISSING
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return RsvpVerdict.CLOSED
            entries = await self._entries(session, row.id)
            change = apply_rsvp(entries, user_id=int(member.id), status=status, seats=int(row.seats), now=moment)
            if change.verdict is RsvpVerdict.ALREADY:
                return change.verdict
            await self._write_change(session, row.id, int(member.id), change, moment)
        self._schedule_flush(int(gathering_id))
        return change.verdict

    async def checkin(
        self,
        guild: discord.Guild,
        gathering_id: int,
        member: Any,
        *,
        now: datetime | None = None,
    ) -> RsvpVerdict:
        if bool(getattr(member, "bot", False)):
            return RsvpVerdict.BOT
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild.id:
                return RsvpVerdict.MISSING
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return RsvpVerdict.CLOSED
            entries = await self._entries(session, row.id)
            change = apply_checkin(entries, user_id=int(member.id), starts_at=row.starts_at, now=moment)
            if change.verdict is not RsvpVerdict.CHECKED:
                return change.verdict
            await self._write_change(session, row.id, int(member.id), change, moment)
        self._schedule_flush(int(gathering_id))
        return change.verdict

    async def cancel(
        self,
        guild: discord.Guild,
        gathering_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return ManageVerdict.ALREADY
            row.cancelled_at = moment
            row.closed_by = actor_id
            card = await self._card(session, row)
        self._cancel_jobs(int(gathering_id))
        await self._edit(guild, card, cancelled_view(card))
        bump_pulse(self._bot, guild.id, now=moment)
        return ManageVerdict.OK

    async def ping(
        self,
        guild: discord.Guild,
        gathering_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return ManageVerdict.MISSING
            if not ping_ready(row.last_ping_at, moment):
                return ManageVerdict.COOLDOWN
            going = await self._count_rsvp(session, row.id, status=RsvpStatus.GOING.value, waitlisted=False)
            if going == 0:
                return ManageVerdict.EMPTY
            ids = await self._rsvp_ids(session, row.id, status=RsvpStatus.GOING.value, waitlisted=False)
            card = await self._card(session, row)
            row.last_ping_at = moment
        channel = guild.get_channel(card.channel_id)
        send = getattr(channel, "send", None)
        if not callable(send):
            return ManageVerdict.MISSING
        try:
            report = await self._discord(ping_many(self._bot, channel, ids, header=ping_view(card), progress=progress))
        except discord.HTTPException as exc:
            log.warning("gathering_ping_failed", gathering_id=card.gathering_id, error=str(exc))
            return ManageVerdict.MISSING
        if report.sent == 0 and report.users == 0:
            return ManageVerdict.MISSING
        return ManageVerdict.OK

    async def open_voice(
        self,
        guild: discord.Guild,
        gathering_id: int,
        *,
        actor_id: int,
        has_manage: bool,
    ) -> tuple[ManageVerdict, int | None]:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING, None
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED, None
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return ManageVerdict.MISSING, None
            host_id = int(row.owner_id)
            stored = None if row.voice_channel_id is None else int(row.voice_channel_id)
        if stored is not None:
            return ManageVerdict.ALREADY, stored
        member = guild.get_member(host_id)
        rooms = getattr(self._bot, "rooms", None)
        create_for = getattr(rooms, "create_for", None)
        if member is None or not callable(create_for):
            return ManageVerdict.NO_HUB, None
        channel_id = created_channel_id(await create_for(member))
        if channel_id is None:
            return ManageVerdict.NO_HUB, None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None:
                return ManageVerdict.MISSING, None
            row.voice_channel_id = int(channel_id)
        return ManageVerdict.OK, int(channel_id)

    async def card(self, guild_id: int, gathering_id: int) -> GatheringCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None or row.guild_id != guild_id:
                return None
            return await self._card(session, row)

    async def flush_card(self, gathering_id: int, *, now: datetime | None = None) -> None:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, int(gathering_id))
            if row is None:
                return
            if gathering_state(cancelled_at=row.cancelled_at) is GatheringState.CANCELLED:
                return
            card = await self._card(session, row)
            guild_id = row.guild_id
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        await self._edit(guild, card, live_view(card, now=moment))

    async def restore(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(Gathering).where(Gathering.cancelled_at.is_(None)))).all()
            pending = [(int(row.id), int(row.guild_id), as_utc(row.starts_at)) for row in rows if row.message_id]
        for gathering_id, guild_id, starts_at in pending:
            if not guild_is_active(self._bot, guild_id):
                continue
            self._schedule_flush(gathering_id)
            self._schedule_start(gathering_id, max(0.0, (starts_at - moment).total_seconds()))
        return len(pending)

    async def open_in_channel(self, guild_id: int, channel_id: int) -> GatheringCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Gathering)
                .where(
                    Gathering.guild_id == guild_id,
                    Gathering.channel_id == channel_id,
                    Gathering.cancelled_at.is_(None),
                )
                .order_by(Gathering.created_at.desc())
            )
            if row is None:
                return None
            return await self._card(session, row)

    async def guard_resource(self, guild_id: int, ref: int | str, *, need: str = "open") -> GuardResource | None:
        try:
            gathering_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Gathering, gathering_id)
            if row is None or row.guild_id != guild_id:
                return None
            state = gathering_state(cancelled_at=row.cancelled_at)
            closed = True
            if need == "open":
                closed = state is not GatheringState.OPEN
            return GuardResource(
                ref=str(row.id),
                guild_id=row.guild_id,
                owner_id=row.owner_id,
                closed=closed,
                channel_id=row.channel_id,
            )

    def _schedule_flush(self, gathering_id: int) -> None:
        cards = getattr(self._bot, "cards", None)
        request = getattr(cards, "request", None)
        if callable(request):
            request(("gathering", int(gathering_id)), lambda: self.flush_card(gathering_id), window=FLUSH_INTERVAL)
            return
        queue = getattr(self._bot, "queue", None)
        until = getattr(queue, "until", None)
        touch = getattr(queue, "touch", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(until) or not callable(touch) or not callable(schedule):
            return
        if until(("ev-count", gathering_id)) > 0:
            return
        touch(("ev-count", gathering_id), FLUSH_INTERVAL)
        schedule(("ev-flush", gathering_id), FLUSH_INTERVAL, lambda gid=gathering_id: self.flush_card(gid))

    def _schedule_start(self, gathering_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("ev-start", gathering_id), delay, lambda gid=gathering_id: self.flush_card(gid))

    def _cancel_jobs(self, gathering_id: int) -> None:
        cards = getattr(self._bot, "cards", None)
        drop = getattr(cards, "cancel", None)
        if callable(drop):
            drop(("gathering", int(gathering_id)))
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if not callable(cancel):
            return
        cancel(("ev-flush", gathering_id))
        cancel(("ev-start", gathering_id))

    async def _edit(self, guild: discord.Guild, card: GatheringCard, view: Any) -> None:
        message = self._message(guild, card)
        if message is None:
            return
        try:
            await self._discord(edit_card(message, view))
        except discord.HTTPException as exc:
            log.warning("gathering_edit_failed", gathering_id=card.gathering_id, error=str(exc))

    def _message(self, guild: discord.Guild, card: GatheringCard) -> Any | None:
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

    async def _card(self, session: AsyncSession, row: Gathering) -> GatheringCard:
        gathering_id = int(row.id)
        return GatheringCard(
            gathering_id=gathering_id,
            title=row.title,
            host_id=int(row.owner_id),
            starts_at=as_utc(row.starts_at),
            seats=int(row.seats),
            entries=await self._entries(session, gathering_id),
            state=gathering_state(cancelled_at=row.cancelled_at),
            channel_id=int(row.channel_id),
            message_id=None if row.message_id is None else int(row.message_id),
            voice_channel_id=None if row.voice_channel_id is None else int(row.voice_channel_id),
            last_ping_at=None if row.last_ping_at is None else as_utc(row.last_ping_at),
            seated_n=await self._count_rsvp(session, gathering_id, status=RsvpStatus.GOING.value, waitlisted=False),
            waiting_n=await self._count_rsvp(session, gathering_id, status=RsvpStatus.GOING.value, waitlisted=True),
            maybe_n=await self._count_rsvp(session, gathering_id, status=RsvpStatus.MAYBE.value),
            cant_n=await self._count_rsvp(session, gathering_id, status=RsvpStatus.CANT.value),
        )

    async def _entries(self, session: AsyncSession, gathering_id: int) -> tuple[Rsvp, ...]:
        rows = (await session.scalars(select(GatheringRsvp).where(GatheringRsvp.gathering_id == gathering_id))).all()
        found: list[Rsvp] = []
        for row in rows:
            try:
                status = RsvpStatus(row.status)
            except ValueError:
                continue
            found.append(
                Rsvp(
                    user_id=int(row.user_id),
                    status=status,
                    waitlisted=bool(row.waitlisted),
                    created_at=as_utc(row.created_at),
                    checked_in_at=None if row.checked_in_at is None else as_utc(row.checked_in_at),
                )
            )
        return tuple(found)

    async def _count_rsvp(
        self,
        session: AsyncSession,
        gathering_id: int,
        *,
        status: str,
        waitlisted: bool | None = None,
    ) -> int:
        query = select(func.count()).select_from(GatheringRsvp).where(
            GatheringRsvp.gathering_id == int(gathering_id),
            GatheringRsvp.status == str(status),
        )
        if waitlisted is not None:
            query = query.where(GatheringRsvp.waitlisted.is_(waitlisted))
        value = await session.scalar(query)
        return int(value or 0)

    async def _rsvp_ids(
        self,
        session: AsyncSession,
        gathering_id: int,
        *,
        status: str,
        waitlisted: bool | None = None,
    ) -> list[int]:
        query = select(GatheringRsvp.user_id).where(
            GatheringRsvp.gathering_id == int(gathering_id),
            GatheringRsvp.status == str(status),
        )
        if waitlisted is not None:
            query = query.where(GatheringRsvp.waitlisted.is_(waitlisted))
        rows = (await session.scalars(query.order_by(GatheringRsvp.created_at, GatheringRsvp.user_id))).all()
        return [int(item) for item in rows]

    async def _write_change(
        self,
        session: AsyncSession,
        gathering_id: int,
        user_id: int,
        change: Any,
        moment: datetime,
    ) -> None:
        by_id = {item.user_id: item for item in change.entries}
        wanted = {user_id}
        if change.promoted_id is not None:
            wanted.add(int(change.promoted_id))
        for target in wanted:
            item = by_id.get(target)
            if item is None:
                continue
            row = await session.scalar(
                select(GatheringRsvp).where(GatheringRsvp.gathering_id == gathering_id, GatheringRsvp.user_id == target)
            )
            if row is None:
                session.add(
                    GatheringRsvp(
                        gathering_id=gathering_id,
                        user_id=target,
                        status=str(item.status),
                        waitlisted=item.waitlisted,
                        checked_in_at=item.checked_in_at,
                        created_at=item.created_at or moment,
                        updated_at=moment,
                    )
                )
                continue
            row.status = str(item.status)
            row.waitlisted = item.waitlisted
            row.checked_in_at = item.checked_in_at
            if item.created_at is not None:
                row.created_at = item.created_at
            row.updated_at = moment

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
