"""Выдача и отзыв рекрут-ссылок. Discord — только после commit (И5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from conductor.db.models import GuildConfig, MemberRecord, TicketPanel, VoiceHub
from conductor.db.models import RecruitLink as RecruitLinkRow
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.privacy import FORGOTTEN_USER_ID
from conductor.domain.recruit import (
    IssuedLink,
    RecruitCard,
    RecruitLink,
    RecruitListing,
    RecruitRow,
    RecruitSummary,
    active_link,
    channel_blocked,
    invite_url,
    page_slice,
    parse_code,
    summarize,
)
from conductor.domain.tickets import parse_ticket_settings
from conductor.presentation import copy
from conductor.services.guild_config import get_or_create_config

log = structlog.get_logger("conductor.recruit")


def _now(now: datetime | None) -> datetime:
    return as_utc(now or datetime.now(UTC))


def _view(row: RecruitLinkRow) -> RecruitLink:
    return RecruitLink(
        id=int(row.id),
        guild_id=int(row.guild_id),
        code=str(row.code),
        owner_user_id=int(row.owner_user_id),
        channel_id=int(row.channel_id),
        created_by=int(row.created_by),
        created_at=as_utc(row.created_at),
        revoked_at=None if row.revoked_at is None else as_utc(row.revoked_at),
        revoked_by=None if row.revoked_by is None else int(row.revoked_by),
    )


class RecruitService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def channel_id(self, guild_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(GuildConfig, int(guild_id))
        if row is None or row.recruit_channel_id is None:
            return None
        return int(row.recruit_channel_id)

    async def channel_error(self, guild_id: int, channel_id: int) -> str | None:
        blocked = await self._blocked_ids(guild_id)
        if channel_blocked(channel_id, blocked):
            return copy.RECRUIT_CHANNEL_BLOCKED
        return None

    async def set_channel(self, guild_id: int, channel_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await get_or_create_config(session, int(guild_id))
            before = None if row.recruit_channel_id is None else int(row.recruit_channel_id)
            row.recruit_channel_id = int(channel_id)
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh):
            await refresh(int(guild_id))
        return before

    async def issue(
        self,
        guild: discord.Guild,
        owner_id: int,
        *,
        actor_id: int,
        channel: discord.abc.GuildChannel | None = None,
        now: datetime | None = None,
    ) -> tuple[IssuedLink | None, str | None]:
        target = channel
        if target is None:
            default_id = await self.channel_id(guild.id)
            getter = getattr(guild, "get_channel", None)
            target = getter(int(default_id)) if callable(getter) and default_id else None
        if target is None:
            return None, copy.RECRUIT_NO_CHANNEL
        error = await self.channel_error(guild.id, int(target.id))
        if error is not None:
            return None, error
        create = getattr(target, "create_invite", None)
        if not callable(create):
            return None, copy.RECRUIT_INVITE_FAILED
        existing = await self._active_for(guild.id, owner_id)
        live = await self._live_codes(guild)
        if existing is not None and (live is None or existing.code in live):
            return IssuedLink(link=existing, url=invite_url(existing.code), reused=True), None
        if existing is not None:
            await self._mark_revoked(existing.id, actor_id=FORGOTTEN_USER_ID, now=now)
        try:
            invite = await self._discord(create(max_age=0, max_uses=0, unique=True, reason=copy.RECRUIT_INVITE_REASON))
        except discord.HTTPException as exc:
            log.warning("recruit_invite_failed", guild_id=guild.id, owner_id=owner_id, error=str(exc))
            return None, copy.RECRUIT_INVITE_FAILED
        code = str(getattr(invite, "code", "") or "")
        url = str(getattr(invite, "url", "") or invite_url(code))
        if not code:
            return None, copy.RECRUIT_INVITE_FAILED
        stamp = _now(now)
        try:
            async with session_scope(self._bot.sessions) as session:
                raced = await self._load_active(session, guild.id, owner_id)
                if raced is not None:
                    extra = code
                    view = _view(raced)
                else:
                    extra = None
                    row = RecruitLinkRow(
                        guild_id=int(guild.id),
                        code=code,
                        owner_user_id=int(owner_id),
                        channel_id=int(target.id),
                        created_by=int(actor_id),
                        created_at=stamp,
                    )
                    session.add(row)
                    await session.flush()
                    view = _view(row)
        except IntegrityError:
            await self._delete_invite(guild, code)
            kept = await self._active_for(guild.id, owner_id)
            if kept is None:
                return None, copy.RECRUIT_INVITE_FAILED
            return IssuedLink(link=kept, url=invite_url(kept.code), reused=True), None
        if extra is not None:
            await self._delete_invite(guild, extra)
            return IssuedLink(link=view, url=invite_url(view.code), reused=True), None
        return IssuedLink(link=view, url=url, reused=False), None

    async def revoke(
        self,
        guild: discord.Guild,
        owner_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> tuple[RecruitLink | None, str | None]:
        existing = await self._active_for(guild.id, owner_id)
        if existing is None:
            return None, copy.RECRUIT_NO_LINK
        await self._delete_invite(guild, existing.code)
        updated = await self._mark_revoked(existing.id, actor_id=int(actor_id), now=now)
        return updated, None

    async def report_listing(self, guild: discord.Guild, *, page: int = 1) -> RecruitListing:
        rows = await self._rows(guild.id)
        chunk, current, pages = page_slice(rows, page)
        return RecruitListing(rows=chunk, page=current, pages=pages)

    async def report_one(
        self,
        guild: discord.Guild,
        *,
        code: str | None = None,
        owner_id: int | None = None,
    ) -> RecruitCard | None:
        await self._reconcile(guild)
        token = parse_code(code)
        link: RecruitLink | None = None
        if token:
            link = await self._by_code(guild.id, token)
        elif owner_id is not None:
            links = await self._links(guild.id)
            link = active_link(links, int(owner_id)) or next(
                (item for item in links if int(item.owner_user_id) == int(owner_id)),
                None,
            )
        if link is None:
            return None
        summary = await self._summary(guild.id, link.code)
        return RecruitCard(link=link, summary=summary, url=invite_url(link.code))

    async def _summary(self, guild_id: int, code: str) -> RecruitSummary:
        async with session_scope(self._bot.sessions) as session:
            people = (
                await session.scalars(
                    select(MemberRecord).where(
                        MemberRecord.guild_id == int(guild_id),
                        MemberRecord.invite_code == code,
                        MemberRecord.is_bot.is_(False),
                    )
                )
            ).all()
        return summarize(people, code)

    async def _rows(self, guild_id: int) -> tuple[RecruitRow, ...]:
        links = await self._links(guild_id)
        rows: list[RecruitRow] = []
        for link in links:
            summary = await self._summary(guild_id, link.code)
            rows.append(
                RecruitRow(
                    code=link.code,
                    owner_user_id=link.owner_user_id,
                    joined_total=summary.joined_total,
                    active=summary.active,
                    present=summary.present,
                    revoked=link.revoked,
                    invite_deleted=link.invite_deleted,
                )
            )
        return tuple(rows)

    async def _links(self, guild_id: int) -> tuple[RecruitLink, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(RecruitLinkRow)
                    .where(RecruitLinkRow.guild_id == int(guild_id))
                    .order_by(RecruitLinkRow.revoked_at.is_not(None), RecruitLinkRow.created_at.desc())
                )
            ).all()
        return tuple(_view(row) for row in rows)

    async def _by_code(self, guild_id: int, code: str) -> RecruitLink | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(RecruitLinkRow).where(RecruitLinkRow.guild_id == int(guild_id), RecruitLinkRow.code == code)
            )
        return None if row is None else _view(row)

    async def _active_for(self, guild_id: int, owner_id: int) -> RecruitLink | None:
        async with session_scope(self._bot.sessions) as session:
            row = await self._load_active(session, guild_id, owner_id)
        return None if row is None else _view(row)

    async def _load_active(self, session: Any, guild_id: int, owner_id: int) -> RecruitLinkRow | None:
        return await session.scalar(
            select(RecruitLinkRow).where(
                RecruitLinkRow.guild_id == int(guild_id),
                RecruitLinkRow.owner_user_id == int(owner_id),
                RecruitLinkRow.revoked_at.is_(None),
            )
        )

    async def _mark_revoked(self, link_id: int, *, actor_id: int, now: datetime | None = None) -> RecruitLink | None:
        stamp = _now(now)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(RecruitLinkRow, int(link_id))
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = stamp
                row.revoked_by = int(actor_id)
            return _view(row)

    async def _reconcile(self, guild: discord.Guild) -> None:
        live = await self._live_codes(guild)
        if live is None:
            return
        stamp = _now(None)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(RecruitLinkRow).where(
                        RecruitLinkRow.guild_id == int(guild.id),
                        RecruitLinkRow.revoked_at.is_(None),
                    )
                )
            ).all()
            for row in rows:
                if row.code not in live:
                    row.revoked_at = stamp
                    row.revoked_by = FORGOTTEN_USER_ID

    async def _blocked_ids(self, guild_id: int) -> set[int]:
        ids: set[int] = set()
        async with session_scope(self._bot.sessions) as session:
            config = await session.get(GuildConfig, int(guild_id))
            if config is not None:
                for name in (
                    "user_log_channel_id",
                    "admin_log_channel_id",
                    "staff_feed_channel_id",
                    "automod_channel_id",
                ):
                    value = getattr(config, name)
                    if value:
                        ids.add(int(value))
            hub = await session.get(VoiceHub, int(guild_id))
            if hub is not None:
                ids.add(int(hub.channel_id))
            panels = (await session.scalars(select(TicketPanel).where(TicketPanel.guild_id == int(guild_id)))).all()
            for panel in panels:
                settings = parse_ticket_settings(panel.settings)
                if settings.log_channel_id:
                    ids.add(int(settings.log_channel_id))
        return ids

    async def _live_codes(self, guild: discord.Guild) -> set[str] | None:
        fetch = getattr(guild, "invites", None)
        if not callable(fetch):
            return None
        try:
            invites = await self._discord(fetch())
        except discord.HTTPException:
            return None
        if invites is None:
            return None
        return {str(item.code) for item in invites if getattr(item, "code", None)}

    async def _delete_invite(self, guild: discord.Guild, code: str) -> None:
        fetch = getattr(guild, "invites", None)
        if not callable(fetch):
            return
        try:
            invites = await self._discord(fetch())
        except discord.HTTPException:
            return
        if not invites:
            return
        for invite in invites:
            if str(getattr(invite, "code", "")) != code:
                continue
            delete = getattr(invite, "delete", None)
            if not callable(delete):
                return
            try:
                await self._discord(delete(reason=copy.RECRUIT_INVITE_REASON))
            except discord.HTTPException:
                return
            return

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
