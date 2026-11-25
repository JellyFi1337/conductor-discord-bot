"""Опросы: срок из БД, анонимность на карточке, раскрытие только явно."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import Poll, PollSettings
from conductor.db.models import PollVote as VoteRow
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.poll import (
    FLUSH_INTERVAL,
    ManageVerdict,
    PollCard,
    PollKind,
    PollSpec,
    PollState,
    PollVote,
    VoteVerdict,
    apply_vote,
    can_manage,
    dump_options,
    load_options,
    parse_kind,
    poll_state,
)
from conductor.domain.self_roles import OptionTag
from conductor.domain.staff_feed import ACTION_POLL_REVEAL
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import edit_card, panel, send_card
from conductor.presentation.mentions import role_mentions
from conductor.presentation.poll import PANEL_KIND, closed_view, live_view, reveal_view
from conductor.presentation.tokens import Tone
from conductor.services.panels import record_panel
from conductor.services.runtime import guild_is_active, log_tick
from conductor.services.staff_feed import actor_rank_of, emit_staff

log = structlog.get_logger("conductor.poll")

TICK_PERIOD = 60.0


@dataclass(frozen=True, slots=True)
class PollStartResult:
    poll_id: int
    channel_id: int
    message_id: int
    jump_url: str
    redirected: bool


class PollService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(TICK_PERIOD, "poll-due", self.tick_due)

    async def settings(self, guild_id: int) -> tuple[int | None, int | None]:
        channel_id, ping_role_id, _exists = await self._load_settings(guild_id)
        return channel_id, ping_role_id

    async def set_channel(self, guild_id: int, channel_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PollSettings, int(guild_id))
            before = None if row is None else row.channel_id
            if row is None:
                session.add(PollSettings(guild_id=int(guild_id), channel_id=int(channel_id)))
            else:
                row.channel_id = int(channel_id)
        return None if before is None else int(before)

    async def set_ping_role(self, guild_id: int, role_id: int | None) -> int | None:
        stored = None if role_id is None else int(role_id)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PollSettings, int(guild_id))
            before = None if row is None else row.ping_role_id
            if row is None:
                session.add(PollSettings(guild_id=int(guild_id), ping_role_id=stored))
            else:
                row.ping_role_id = stored
        return None if before is None else int(before)

    async def start(
        self,
        guild: discord.Guild,
        channel: Any,
        *,
        actor_id: int,
        spec: PollSpec,
        now: datetime | None = None,
    ) -> PollStartResult | None:
        configured_id, stored_ping, exists = await self._load_settings(guild.id)
        target, redirected = self._resolve_channel(guild, channel, configured_id)
        send = getattr(target, "send", None)
        if not callable(send) or target is None:
            return None
        ping_role_id = await self._ping_role(guild.id, stored_ping, exists)
        moment = as_utc(now or datetime.now(UTC))
        ends_at = moment + spec.duration
        async with session_scope(self._bot.sessions) as session:
            row = Poll(
                guild_id=guild.id,
                channel_id=int(target.id),
                message_id=None,
                owner_id=actor_id,
                question=spec.question,
                kind=str(spec.kind),
                allow_change=spec.allow_change,
                options_json=dump_options(spec.options),
                ends_at=ends_at,
                created_at=moment,
            )
            session.add(row)
            await session.flush()
            poll_id = int(row.id)
        card = PollCard(
            poll_id=poll_id,
            question=spec.question,
            host_id=actor_id,
            kind=spec.kind,
            allow_change=spec.allow_change,
            options=spec.options,
            ends_at=ends_at,
            channel_id=int(target.id),
        )
        try:
            message = await self._discord(
                send_card(target, live_view(card, ping_role_id=ping_role_id), mentions=role_mentions(ping_role_id))
            )
        except discord.HTTPException as exc:
            log.warning("poll_send_failed", guild_id=guild.id, error=str(exc))
            async with session_scope(self._bot.sessions) as session:
                stale = await session.get(Poll, poll_id)
                if stale is not None:
                    await session.delete(stale)
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, poll_id)
            if row is None:
                return None
            row.message_id = int(message.id)
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=int(target.id),
                message_id=int(message.id),
                ref=poll_id,
                created_by=actor_id,
                now=moment,
            )
        self._schedule_end(poll_id, max(0.0, (ends_at - moment).total_seconds()))
        jump = getattr(message, "jump_url", None) or (
            f"https://discord.com/channels/{guild.id}/{int(target.id)}/{int(message.id)}"
        )
        return PollStartResult(
            poll_id=poll_id,
            channel_id=int(target.id),
            message_id=int(message.id),
            jump_url=str(jump),
            redirected=redirected,
        )

    async def vote(
        self,
        guild: discord.Guild,
        poll_id: int,
        member: Any,
        choice: int,
        *,
        now: datetime | None = None,
    ) -> VoteVerdict:
        if bool(getattr(member, "bot", False)):
            return VoteVerdict.BOT
        moment = as_utc(now or datetime.now(UTC))
        try:
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(Poll, int(poll_id))
                if row is None or row.guild_id != guild.id:
                    return VoteVerdict.MISSING
                state = poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at)
                existing = await session.scalar(
                    select(VoteRow).where(VoteRow.poll_id == row.id, VoteRow.user_id == int(member.id))
                )
                previous = None if existing is None else int(existing.option_index)
                options = load_options(row.options_json)
                verdict = apply_vote(
                    previous=previous,
                    choice=choice,
                    option_count=len(options),
                    allow_change=bool(row.allow_change),
                    open=state is PollState.OPEN,
                )
                if verdict is VoteVerdict.CAST:
                    session.add(
                        VoteRow(
                            poll_id=row.id,
                            user_id=int(member.id),
                            option_index=choice,
                            created_at=moment,
                            updated_at=moment,
                        )
                    )
                elif verdict is VoteVerdict.CHANGED and existing is not None:
                    existing.option_index = choice
                    existing.updated_at = moment
                else:
                    return verdict
        except IntegrityError:
            return VoteVerdict.ALREADY
        self._schedule_flush(int(poll_id))
        return verdict

    async def close(
        self,
        guild: discord.Guild,
        poll_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, int(poll_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            state = poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at)
            if state is PollState.DELETED:
                return ManageVerdict.MISSING
            if state is PollState.CLOSED:
                return ManageVerdict.ALREADY
            row.closed_at = moment
            row.closed_by = actor_id
            card = await self._card(session, row)
        self._cancel_jobs(int(poll_id))
        await self._edit(guild, card, closed_view(card))
        return ManageVerdict.OK

    async def delete(
        self,
        guild: discord.Guild,
        poll_id: int,
        *,
        actor_id: int,
        has_manage: bool,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, int(poll_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if not can_manage(actor_id=actor_id, host_id=row.owner_id, has_manage=has_manage):
                return ManageVerdict.DENIED
            if row.deleted_at is not None:
                return ManageVerdict.ALREADY
            row.deleted_at = moment
            row.closed_by = actor_id
            if row.closed_at is None:
                row.closed_at = moment
            card = await self._card(session, row)
        self._cancel_jobs(int(poll_id))
        message = self._message(guild, card)
        if message is not None:
            delete = getattr(message, "delete", None)
            if callable(delete):
                try:
                    await self._discord(delete())
                except discord.HTTPException as exc:
                    log.warning("poll_delete_failed", poll_id=card.poll_id, error=str(exc))
        return ManageVerdict.OK

    async def deanonymize(
        self,
        guild: discord.Guild,
        poll_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, int(poll_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            kind = parse_kind(row.kind) or PollKind.PUBLIC
            if kind is not PollKind.ANONYMOUS:
                return ManageVerdict.NOT_ANON
            state = poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at)
            if state is PollState.OPEN:
                return ManageVerdict.OPEN
            if row.deanonymized_at is not None:
                return ManageVerdict.ALREADY
            row.deanonymized_at = moment
            row.deanonymized_by = actor_id
            card = await self._card(session, row)
        await self._announce_reveal(guild, card, actor_id=actor_id)
        return ManageVerdict.OK

    async def flush_card(self, poll_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, int(poll_id))
            if row is None:
                return
            if poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at) is not PollState.OPEN:
                return
            card = await self._card(session, row)
            guild_id = row.guild_id
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        _channel_id, stored_ping, exists = await self._load_settings(guild_id)
        ping_role_id = await self._ping_role(guild_id, stored_ping, exists)
        await self._edit(guild, card, live_view(card, ping_role_id=ping_role_id))

    async def restore(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(select(Poll).where(Poll.closed_at.is_(None), Poll.deleted_at.is_(None)))
            ).all()
            pending = [(int(row.id), int(row.guild_id), as_utc(row.ends_at)) for row in rows if row.message_id]
        scheduled = 0
        for poll_id, guild_id, ends_at in pending:
            if not guild_is_active(self._bot, guild_id):
                continue
            self._schedule_end(poll_id, max(0.0, (ends_at - moment).total_seconds()))
            scheduled += 1
        return len(pending)

    async def tick_due(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            due = (
                await session.execute(
                    select(Poll.id, Poll.guild_id).where(
                        Poll.closed_at.is_(None),
                        Poll.deleted_at.is_(None),
                        Poll.message_id.is_not(None),
                        Poll.ends_at <= moment,
                    )
                )
            ).all()
        done = 0
        for poll_id, guild_id in due:
            if not guild_is_active(self._bot, int(guild_id)):
                continue
            guild = self._bot.get_guild(int(guild_id))
            if guild is None:
                continue
            result = await self.close(guild, int(poll_id), actor_id=0, has_manage=True, now=moment)
            if result is ManageVerdict.OK:
                done += 1
        log_tick(log, "poll_due", changed=done)
        return done

    async def open_in_channel(self, guild_id: int, channel_id: int) -> PollCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Poll)
                .where(
                    Poll.guild_id == guild_id,
                    Poll.channel_id == channel_id,
                    Poll.closed_at.is_(None),
                    Poll.deleted_at.is_(None),
                )
                .order_by(Poll.created_at.desc())
            )
            if row is None:
                return None
            return await self._card(session, row)

    async def latest_anonymous(self, guild_id: int, channel_id: int) -> PollCard | None:
        configured_id, _ping, _exists = await self._load_settings(guild_id)
        seen: list[int] = []
        for candidate in (configured_id, int(channel_id)):
            if candidate is None or int(candidate) in seen:
                continue
            seen.append(int(candidate))
            card = await self._latest_anonymous_in(guild_id, int(candidate))
            if card is not None:
                return card
        return None

    async def _latest_anonymous_in(self, guild_id: int, channel_id: int) -> PollCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Poll)
                .where(
                    Poll.guild_id == guild_id,
                    Poll.channel_id == channel_id,
                    Poll.kind == PollKind.ANONYMOUS,
                )
                .order_by(Poll.created_at.desc())
            )
            if row is None:
                return None
            return await self._card(session, row)

    async def guard_resource(self, guild_id: int, ref: int | str, *, need: str = "open") -> GuardResource | None:
        try:
            poll_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, poll_id)
            if row is None or row.guild_id != guild_id:
                return None
            state = poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at)
            closed = True
            if need == "open":
                closed = state is not PollState.OPEN
            return GuardResource(
                ref=str(row.id),
                guild_id=row.guild_id,
                owner_id=row.owner_id,
                closed=closed,
                channel_id=row.channel_id,
            )

    def _schedule_end(self, poll_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("pl-end", poll_id), delay, lambda pid=poll_id: self._close_due(pid))

    async def _close_due(self, poll_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Poll, int(poll_id))
            guild_id = None if row is None else int(row.guild_id)
        if guild_id is None:
            return
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        await self.close(guild, poll_id, actor_id=0, has_manage=True)

    def _schedule_flush(self, poll_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        until = getattr(queue, "until", None)
        touch = getattr(queue, "touch", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(until) or not callable(touch) or not callable(schedule):
            return
        if until(("pl-count", poll_id)) > 0:
            return
        touch(("pl-count", poll_id), FLUSH_INTERVAL)
        schedule(("pl-flush", poll_id), FLUSH_INTERVAL, lambda pid=poll_id: self.flush_card(pid))

    def _cancel_jobs(self, poll_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if not callable(cancel):
            return
        cancel(("pl-end", poll_id))
        cancel(("pl-flush", poll_id))

    async def _announce_reveal(self, guild: discord.Guild, card: PollCard, *, actor_id: int) -> None:
        channel = guild.get_channel(card.channel_id)
        send = getattr(channel, "send", None)
        if callable(send):
            try:
                await self._discord(send_card(channel, reveal_view(card)))
            except discord.HTTPException as exc:
                log.warning("poll_reveal_failed", poll_id=card.poll_id, error=str(exc))
        self._staff_log(guild.id, copy.poll_reveal_log(actor_id, card.question, len(card.votes)))
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor_id,
            actor_rank=await actor_rank_of(self._bot, guild, actor_id),
            action=ACTION_POLL_REVEAL,
            ref=card.poll_id,
            details=card.question,
        )

    def _staff_log(self, guild_id: int, body: str) -> None:
        configs = getattr(self._bot, "config_by_guild", None) or {}
        config = configs.get(guild_id) if hasattr(configs, "get") else None
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        submit(
            channel_id,
            view=panel(copy.POLL_REVEAL_TITLE, body, tone=Tone.WARN, footnote=copy.WATCHERS_LOG_FOOTNOTE),
        )

    async def _edit(self, guild: discord.Guild, card: PollCard, view: Any) -> None:
        message = self._message(guild, card)
        if message is None:
            return
        try:
            await self._discord(edit_card(message, view))
        except discord.HTTPException as exc:
            log.warning("poll_edit_failed", poll_id=card.poll_id, error=str(exc))

    def _message(self, guild: discord.Guild, card: PollCard) -> Any | None:
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

    async def _card(self, session: AsyncSession, row: Poll) -> PollCard:
        votes = await self._votes(session, int(row.id))
        kind = parse_kind(row.kind) or PollKind.PUBLIC
        return PollCard(
            poll_id=int(row.id),
            question=row.question,
            host_id=int(row.owner_id),
            kind=kind,
            allow_change=bool(row.allow_change),
            options=load_options(row.options_json),
            ends_at=as_utc(row.ends_at),
            votes=votes,
            state=poll_state(closed_at=row.closed_at, deleted_at=row.deleted_at),
            channel_id=int(row.channel_id),
            message_id=None if row.message_id is None else int(row.message_id),
            closed_at=None if row.closed_at is None else as_utc(row.closed_at),
            deanonymized_at=None if row.deanonymized_at is None else as_utc(row.deanonymized_at),
        )

    async def _votes(self, session: AsyncSession, poll_id: int) -> tuple[PollVote, ...]:
        rows = (await session.scalars(select(VoteRow).where(VoteRow.poll_id == poll_id))).all()
        return tuple(PollVote(user_id=int(row.user_id), option_index=int(row.option_index)) for row in rows)

    async def _load_settings(self, guild_id: int) -> tuple[int | None, int | None, bool]:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PollSettings, int(guild_id))
        if row is None:
            return None, None, False
        return (
            None if row.channel_id is None else int(row.channel_id),
            None if row.ping_role_id is None else int(row.ping_role_id),
            True,
        )

    async def _ping_role(self, guild_id: int, ping_role_id: int | None, exists: bool) -> int | None:
        if not exists:
            return None
        if ping_role_id:
            return int(ping_role_id)
        self_roles = getattr(self._bot, "self_roles", None)
        getter = getattr(self_roles, "role_for_tag", None)
        if not callable(getter):
            return None
        found = await getter(int(guild_id), OptionTag.POLLS.value)
        return None if not found else int(found)

    def _resolve_channel(
        self,
        guild: discord.Guild,
        fallback: Any,
        channel_id: int | None,
    ) -> tuple[Any | None, bool]:
        if channel_id is None:
            send = getattr(fallback, "send", None)
            if not callable(send):
                return None, False
            return fallback, False
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            getter = getattr(guild, "get_thread", None)
            channel = getter(int(channel_id)) if callable(getter) else None
        send = getattr(channel, "send", None)
        if not callable(send):
            return None, False
        fallback_id = getattr(fallback, "id", None)
        redirected = fallback_id is None or int(fallback_id) != int(channel_id)
        return channel, redirected

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
