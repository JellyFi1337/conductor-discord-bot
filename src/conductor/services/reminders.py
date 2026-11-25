"""Напоминания: запись в БД, отложенная доставка, восстановление после рестарта."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import Reminder
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.reminders import (
    PER_USER_LIMIT,
    CancelVerdict,
    CreateVerdict,
    ReminderCard,
    can_accept,
    parse_due,
    parse_text,
)
from conductor.presentation.layout import send_card
from conductor.presentation.reminders import due_mentions, due_view
from conductor.services.runtime import guild_is_active

log = structlog.get_logger("conductor.reminders")


class ReminderService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def create(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        *,
        when: str,
        text: str,
        now: datetime | None = None,
        limit: int = PER_USER_LIMIT,
    ) -> tuple[CreateVerdict, ReminderCard | None]:
        moment = as_utc(now or datetime.now(UTC))
        due = parse_due(when, now=moment)
        if due is None:
            return CreateVerdict.BAD_WHEN, None
        body = parse_text(text)
        if body is None:
            return CreateVerdict.BAD_TEXT, None
        async with session_scope(self._bot.sessions) as session:
            pending = await self._pending_count(session, guild_id, user_id)
            if not can_accept(pending, limit=limit):
                return CreateVerdict.LIMIT, None
            row = Reminder(
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                user_id=int(user_id),
                body=body,
                due_at=due,
                created_at=moment,
            )
            session.add(row)
            await session.flush()
            card = self._card(row)
        self._schedule(card.reminder_id, max(0.0, (due - moment).total_seconds()))
        return CreateVerdict.OK, card

    async def pending(self, guild_id: int, user_id: int) -> tuple[ReminderCard, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(Reminder)
                    .where(*self._open(guild_id=guild_id, user_id=user_id))
                    .order_by(Reminder.due_at.asc(), Reminder.id.asc())
                )
            ).all()
            return tuple(self._card(row) for row in rows)

    async def cancel(
        self,
        guild_id: int,
        user_id: int,
        reminder_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[CancelVerdict, ReminderCard | None]:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Reminder, int(reminder_id))
            if row is None or int(row.guild_id) != int(guild_id) or int(row.user_id) != int(user_id):
                return CancelVerdict.MISSING, None
            if row.delivered_at is not None or row.cancelled_at is not None:
                return CancelVerdict.ALREADY, self._card(row)
            row.cancelled_at = moment
            card = self._card(row)
        self._cancel_job(card.reminder_id)
        return CancelVerdict.OK, card

    async def deliver(self, reminder_id: int, *, now: datetime | None = None) -> bool:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Reminder, int(reminder_id))
            if row is None or row.delivered_at is not None or row.cancelled_at is not None:
                return False
            card = self._card(row)
        guild = self._bot.get_guild(card.guild_id)
        if guild is None:
            self._schedule(card.reminder_id, 30.0)
            return False
        sent = await self._send(guild, card)
        if not sent:
            log.warning("reminder_send_failed", guild_id=card.guild_id, reminder_id=card.reminder_id)
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Reminder, int(reminder_id))
            if row is None or row.delivered_at is not None or row.cancelled_at is not None:
                return False
            row.delivered_at = moment
        log.info("reminder_delivered", guild_id=card.guild_id, reminder_id=card.reminder_id)
        return True

    async def restore(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(Reminder).where(Reminder.delivered_at.is_(None), Reminder.cancelled_at.is_(None))
                )
            ).all()
            pending = [(int(row.id), int(row.guild_id), as_utc(row.due_at)) for row in rows]
        for reminder_id, guild_id, due_at in pending:
            if not guild_is_active(self._bot, guild_id):
                continue
            self._schedule(reminder_id, max(0.0, (due_at - moment).total_seconds()))
        return len(pending)

    async def _pending_count(self, session: AsyncSession, guild_id: int, user_id: int) -> int:
        found = await session.scalar(
            select(func.count()).select_from(Reminder).where(*self._open(guild_id=guild_id, user_id=user_id))
        )
        return int(found or 0)

    def _open(self, *, guild_id: int, user_id: int):
        return (
            Reminder.guild_id == int(guild_id),
            Reminder.user_id == int(user_id),
            Reminder.delivered_at.is_(None),
            Reminder.cancelled_at.is_(None),
        )

    def _card(self, row: Reminder) -> ReminderCard:
        return ReminderCard(
            reminder_id=int(row.id),
            guild_id=int(row.guild_id),
            channel_id=int(row.channel_id),
            user_id=int(row.user_id),
            text=row.body,
            due_at=as_utc(row.due_at),
        )

    def _schedule(self, reminder_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("rm", reminder_id), delay, lambda rid=reminder_id: self.deliver(rid))

    def _cancel_job(self, reminder_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if callable(cancel):
            cancel(("rm", reminder_id))

    async def _send(self, guild: Any, card: ReminderCard) -> bool:
        mentions = due_mentions(card.user_id)
        view = due_view(card)
        channel = guild.get_channel(card.channel_id)
        if await self._try_send(channel, view, mentions):
            return True
        member = guild.get_member(card.user_id) if hasattr(guild, "get_member") else None
        user = member or getattr(self._bot, "get_user", lambda _uid: None)(card.user_id)
        return await self._try_send(user, view, mentions)

    async def _try_send(self, target: Any, view: Any, mentions: discord.AllowedMentions) -> bool:
        if target is None or not callable(getattr(target, "send", None)):
            return False
        try:
            await self._discord(send_card(target, view, mentions=mentions))
        except discord.HTTPException as exc:
            log.warning("reminder_target_failed", error=str(exc))
            return False
        return True

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
