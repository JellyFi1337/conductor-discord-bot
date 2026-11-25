"""Отчёты об ошибках: запись, антиспам, карточка в канал персонала."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import func, select

from conductor import __version__
from conductor.db.models import BugReport, GuildLayout
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.bugs import (
    HOUR_WINDOW,
    BugCard,
    BugMatch,
    BugStatus,
    DecideVerdict,
    SubmitVerdict,
    clip_did,
    clip_happened,
    parse_code,
    rate_ok,
)
from conductor.presentation import copy
from conductor.presentation.bugs import report_view
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import panel, send_card
from conductor.presentation.tokens import Tone
from conductor.services.runtime import guild_is_active

log = structlog.get_logger("conductor.bugs")

_LAYOUT_KEY = "bug_reports"


class SubmitResult:
    __slots__ = ("verdict", "card")

    def __init__(self, verdict: SubmitVerdict, card: BugCard | None = None) -> None:
        self.verdict = verdict
        self.card = card


class DecideResult:
    __slots__ = ("verdict", "card")

    def __init__(self, verdict: DecideVerdict, card: BugCard | None = None) -> None:
        self.verdict = verdict
        self.card = card


class BugService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def submit(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        *,
        did: str,
        happened: str,
        code: str = "",
        now: datetime | None = None,
    ) -> SubmitResult:
        if not guild_is_active(self._bot, guild.id):
            return SubmitResult(SubmitVerdict.INACTIVE)
        stamp = as_utc(now or datetime.now(UTC))
        did_text = clip_did(did)
        happened_text = clip_happened(happened)
        if not did_text or not happened_text:
            return SubmitResult(SubmitVerdict.BAD_TEXT)
        parsed = parse_code(code)
        channel = await self._channel(guild)
        if channel is None:
            return SubmitResult(SubmitVerdict.NO_CHANNEL)
        async with session_scope(self._bot.sessions) as session:
            recent = await session.scalar(
                select(func.count())
                .select_from(BugReport)
                .where(
                    BugReport.guild_id == guild.id,
                    BugReport.user_id == user.id,
                    BugReport.created_at >= stamp - HOUR_WINDOW,
                )
            )
            if not rate_ok(int(recent or 0)):
                return SubmitResult(SubmitVerdict.RATE)
            row = BugReport(
                guild_id=guild.id,
                user_id=user.id,
                code=parsed,
                did=did_text,
                happened=happened_text,
                version=str(__version__),
                status=BugStatus.NEW.value,
                created_at=stamp,
            )
            session.add(row)
            await session.flush()
            report_id = int(row.id)
        match = self._match(guild.id, parsed)
        card = BugCard(
            report_id=report_id,
            guild_id=guild.id,
            user_id=user.id,
            did=did_text,
            happened=happened_text,
            version=str(__version__),
            status=BugStatus.NEW.value,
            created_at=stamp,
            code=parsed or "",
            match=match,
        )
        try:
            await send_card(channel, report_view(card))
        except Exception:
            log.exception("bug_report_post_failed", guild_id=guild.id, report_id=report_id)
        return SubmitResult(SubmitVerdict.OK, card)

    async def decide(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
        ref: str,
        status: BugStatus,
        *,
        now: datetime | None = None,
    ) -> DecideResult:
        if not guild_is_active(self._bot, guild.id):
            return DecideResult(DecideVerdict.MISSING)
        try:
            report_id = int(ref)
        except (TypeError, ValueError):
            return DecideResult(DecideVerdict.MISSING)
        stamp = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(BugReport).where(BugReport.id == report_id, BugReport.guild_id == guild.id)
            )
            if row is None:
                return DecideResult(DecideVerdict.MISSING)
            if row.status != BugStatus.NEW.value:
                return DecideResult(DecideVerdict.CLOSED, self._card_of(row))
            row.status = status.value
            row.decided_by = actor.id
            row.decided_at = stamp
            card = self._card_of(row)
        await self._notify(guild, card)
        return DecideResult(DecideVerdict.OK, card)

    async def guard_report(self, guild_id: int, ref: str) -> GuardResource | None:
        try:
            report_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(BugReport.id).where(BugReport.id == report_id, BugReport.guild_id == int(guild_id))
            )
        if row is None:
            return None
        return GuardResource(ref=str(report_id), guild_id=int(guild_id))

    async def _channel(self, guild: discord.Guild) -> discord.abc.Messageable | None:
        async with session_scope(self._bot.sessions) as session:
            bound = await session.scalar(
                select(GuildLayout.channel_id).where(
                    GuildLayout.guild_id == guild.id, GuildLayout.key == _LAYOUT_KEY
                )
            )
        if bound is None:
            return None
        found = guild.get_channel(int(bound))
        return found if found is not None else None

    def _match(self, guild_id: int, code: str | None) -> BugMatch | None:
        if not code:
            return None
        errors = getattr(self._bot, "errors", None)
        lookup = getattr(errors, "lookup", None)
        if not callable(lookup):
            return None
        found = lookup(guild_id, code)
        return found if isinstance(found, BugMatch) else None

    def _card_of(self, row: BugReport) -> BugCard:
        parsed = parse_code(row.code or "")
        return BugCard(
            report_id=int(row.id),
            guild_id=int(row.guild_id),
            user_id=int(row.user_id),
            did=row.did,
            happened=row.happened,
            version=row.version,
            status=row.status,
            created_at=as_utc(row.created_at),
            code=parsed or "",
            match=self._match(int(row.guild_id), parsed),
            decided_by=row.decided_by,
            decided_at=None if row.decided_at is None else as_utc(row.decided_at),
        )

    async def _notify(self, guild: discord.Guild, card: BugCard) -> None:
        member = guild.get_member(card.user_id)
        target = member if member is not None else getattr(self._bot, "get_user", lambda _uid: None)(card.user_id)
        if target is None:
            return
        body = copy.bug_dm_body(card.status)
        try:
            await send_card(target, panel(copy.BUG_TITLE, body, tone=Tone.INFO))
        except Exception:
            log.debug("bug_dm_closed", guild_id=guild.id, user_id=card.user_id)
