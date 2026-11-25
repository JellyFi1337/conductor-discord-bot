"""Приватные группы: категория, перекрытия, архив с историей, тишина."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GroupArchiveShelf, GroupHub, GroupInvite, GroupMember, PrivateGroup, Reputation
from conductor.db.session import session_scope
from conductor.domain.access import (
    GROUP_ARCHIVE_MANAGE,
    GROUP_ARCHIVE_READ,
    GROUP_CREATE,
    Rank,
    decide,
    is_special_or_admin,
    resolve_rank,
)
from conductor.domain.activity import as_utc
from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_DELETE
from conductor.domain.discord_limits import CATEGORY_CHILDREN, GUILD_CHANNELS
from conductor.domain.groups import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MIN_REPUTATION,
    DEFAULT_OPEN_LIMIT,
    DEFAULT_SILENCE,
    HUB_NAME,
    INVITE_COOLDOWN,
    INVITE_TTL,
    STAFF_RANKS,
    SYSTEM_ACTOR,
    ArchiveReason,
    CreateGate,
    CreateReport,
    CreateVerdict,
    GroupCard,
    GroupHubSettings,
    GroupInviteCard,
    GroupListing,
    GroupSpec,
    GroupState,
    InviteSendResult,
    InviteStatus,
    ManageVerdict,
    activity_write_due,
    can_archive,
    can_invite,
    can_leave,
    can_purge,
    can_restore,
    can_send_invite,
    group_channel_name,
    group_quota,
    group_state,
    invite_verdict,
    may_create_group,
    purge_at,
    seconds_until_silence,
    seconds_until_silence_warn,
    silence_deadline,
    silence_due,
    silence_warn_due,
)
from conductor.domain.outcome import Teardown
from conductor.domain.reputation import START
from conductor.domain.seed import LAYOUT
from conductor.domain.staff_feed import ACTION_GROUP_PURGE
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.presentation.groups import PANEL_KIND, archive_header_view, header_view, invite_dm_view
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import edit_card, panel, send_card
from conductor.presentation.tokens import Tone
from conductor.services.access import cached_snapshot
from conductor.services.create_lock import continue_after_hold, hold_for, log_refused, note_success, trip
from conductor.services.panels import mark_stale_by_ref, record_panel
from conductor.services.runtime import guild_is_active, log_tick
from conductor.services.staff_feed import emit_staff
from conductor.services.traffic import lane

log = structlog.get_logger("conductor.groups")

_CREATE_REASON = copy.REASON_GROUP
_ARCHIVE_REASON = copy.REASON_GROUP_ARCHIVE
_PURGE_REASON = copy.REASON_GROUP_PURGE
_RESTORE_REASON = copy.REASON_GROUP_RESTORE


class GroupService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._activity_written: dict[int, datetime] = {}
        self._create_locks: dict[tuple[int, int], asyncio.Lock] = {}

    def _create_lock(self, guild_id: int, owner_id: int) -> asyncio.Lock:
        return self._create_locks.setdefault((int(guild_id), int(owner_id)), asyncio.Lock())

    async def create(
        self,
        guild: discord.Guild,
        actor: Any,
        spec: GroupSpec,
        *,
        now: datetime | None = None,
    ) -> CreateReport:
        moment = as_utc(now or datetime.now(UTC))
        owner_id = int(actor.id)
        async with self._create_lock(guild.id, owner_id):
            return await self._create_locked(guild, actor, spec, moment)

    async def _create_locked(
        self,
        guild: discord.Guild,
        actor: Any,
        spec: GroupSpec,
        moment: datetime,
    ) -> CreateReport:
        owner_id = int(actor.id)
        has_key, privileged, settings, score, created_at, open_count = await self._create_gate(
            guild, actor, moment
        )
        gate = may_create_group(
            has_key=has_key,
            privileged=privileged,
            score=score,
            min_reputation=settings.min_reputation,
            reputation_enabled=True,
        )
        if gate is CreateGate.REPUTATION:
            return CreateReport(
                CreateVerdict.REPUTATION,
                score=score,
                min_reputation=settings.min_reputation,
                title=spec.title,
            )
        if gate is CreateGate.REPUTATION_OFF:
            return CreateReport(CreateVerdict.REPUTATION_OFF, title=spec.title)
        quota = group_quota(
            created_at=created_at,
            open_count=open_count,
            now=moment,
            daily_limit=settings.daily_limit,
            open_limit=settings.open_limit,
            privileged=privileged,
        )
        if not quota.ok and quota.reason == "daily":
            return CreateReport(
                CreateVerdict.QUOTA_DAILY,
                retry_at=quota.retry_at,
                title=spec.title,
                open_count=open_count,
                open_limit=settings.open_limit,
            )
        if not quota.ok:
            return CreateReport(
                CreateVerdict.QUOTA_OPEN,
                title=spec.title,
                open_count=open_count,
                open_limit=settings.open_limit,
            )
        category = await self._ensure_category(guild)
        if category is None:
            return CreateReport(CreateVerdict.FAILED, title=spec.title)
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _overwrites(guild, (owner_id,), snapshot)
        name = group_channel_name(spec.title)
        hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
        if hold is not None:
            log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            continue_after_hold(
                self._bot,
                ("group-create-defer", guild.id, owner_id),
                hold,
                lambda: self.create(guild, actor, spec, now=moment),
            )
            return CreateReport(CreateVerdict.LOCKED, title=spec.title)
        try:
            with lane(Lane.USER):
                channel = await self._discord(
                    guild.create_text_channel(
                        name,
                        category=category,
                        overwrites=overwrites,
                        reason=copy.actor_reason(owner_id, _CREATE_REASON),
                    )
                )
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            log.warning("group_text_failed", guild_id=guild.id, error=str(exc))
            return CreateReport(CreateVerdict.FAILED, title=spec.title)
        note_success(self._bot, guild.id, BUCKET_CHANNEL)
        voice = None
        if spec.with_voice:
            hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
                await self._safe_delete(channel)
                return CreateReport(CreateVerdict.LOCKED, title=spec.title)
            try:
                with lane(Lane.USER):
                    voice = await self._discord(
                        guild.create_voice_channel(
                            name,
                            category=category,
                            overwrites=overwrites,
                            reason=copy.actor_reason(owner_id, _CREATE_REASON),
                        )
                    )
            except (discord.HTTPException, discord.RateLimited) as exc:
                hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
                if hold is not None:
                    log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
                log.warning("group_voice_failed", guild_id=guild.id, error=str(exc))
                await self._safe_delete(channel)
                return CreateReport(CreateVerdict.FAILED, title=spec.title)
            note_success(self._bot, guild.id, BUCKET_CHANNEL)
        voice_id = None if voice is None else int(voice.id)
        invitees = tuple(user_id for user_id in spec.member_ids if int(user_id) != owner_id)
        async with session_scope(self._bot.sessions) as session:
            row = PrivateGroup(
                guild_id=guild.id,
                owner_id=owner_id,
                title=spec.title,
                channel_id=int(channel.id),
                voice_channel_id=voice_id,
                created_at=moment,
                last_activity_at=moment,
            )
            session.add(row)
            await session.flush()
            group_id = int(row.id)
            session.add(GroupMember(group_id=group_id, user_id=owner_id))
            for user_id in invitees:
                session.add(
                    GroupInvite(
                        group_id=group_id,
                        user_id=int(user_id),
                        invited_by=owner_id,
                        created_at=moment,
                        expires_at=moment + INVITE_TTL,
                        status=InviteStatus.PENDING.value,
                        dm_sent=False,
                    )
                )
            silence = await self._silence_for(session, guild.id)
        try:
            message = await self._discord(
                send_card(
                    channel,
                    header_view(
                        GroupCard(
                            group_id=group_id,
                            title=spec.title,
                            owner_id=owner_id,
                            member_ids=(owner_id,),
                            channel_id=int(channel.id),
                            voice_channel_id=voice_id,
                            created_at=moment,
                            last_activity_at=moment,
                            silence_after=silence,
                        )
                    ),
                )
            )
        except discord.HTTPException as exc:
            log.warning("group_header_failed", guild_id=guild.id, error=str(exc))
            await self._safe_delete(channel)
            await self._safe_delete(voice)
            async with session_scope(self._bot.sessions) as session:
                stale = await session.get(PrivateGroup, group_id)
                if stale is not None:
                    await session.delete(stale)
            return CreateReport(CreateVerdict.FAILED, title=spec.title)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, group_id)
            if row is None:
                return CreateReport(CreateVerdict.FAILED, title=spec.title)
            row.message_id = int(message.id)
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=int(channel.id),
                message_id=int(message.id),
                ref=group_id,
                created_by=owner_id,
                now=moment,
            )
        self._schedule_silence(group_id, seconds_until_silence(moment, moment, silence_after=silence))
        self._schedule_warn(group_id, seconds_until_silence_warn(moment, moment, silence_after=silence))
        sent, closed = await self._deliver_invites(guild, group_id, spec.title, owner_id, invitees)
        return CreateReport(
            CreateVerdict.OK,
            channel_id=int(channel.id),
            title=spec.title,
            invites_sent=sent,
            invites_closed=closed,
            open_count=open_count + 1,
            open_limit=settings.open_limit,
        )

    async def invite(
        self,
        guild: discord.Guild,
        group_id: int,
        *,
        actor_id: int,
        user_id: int,
        now: datetime | None = None,
    ) -> InviteSendResult:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild.id:
                return InviteSendResult(ManageVerdict.MISSING)
            if group_state(archived_at=row.archived_at) is GroupState.ARCHIVED:
                return InviteSendResult(ManageVerdict.MISSING)
            if not can_invite(actor_id=actor_id, owner_id=row.owner_id):
                return InviteSendResult(ManageVerdict.DENIED)
            members = await self._member_ids(session, row.id)
            pending = await self._pending_ids(session, row.id)
            existing_row = await session.get(GroupInvite, (int(group_id), int(user_id)))
            existing = None
            decided_at = None
            if existing_row is not None:
                try:
                    existing = InviteStatus(existing_row.status)
                except ValueError:
                    existing = None
                decided_at = existing_row.decided_at
            member = guild.get_member(int(user_id))
            is_bot = bool(getattr(member, "bot", False)) if member is not None else False
            pending_count = len(pending)
            if existing is InviteStatus.PENDING and int(user_id) in pending:
                pending_count = max(0, pending_count - 1)
            verdict = can_send_invite(
                user_id=int(user_id),
                member_ids=members,
                is_bot=is_bot,
                existing=existing,
                decided_at=decided_at,
                now=moment,
                pending_count=pending_count,
            )
            if verdict is ManageVerdict.COOLDOWN and decided_at is not None:
                return InviteSendResult(verdict, retry_at=as_utc(decided_at) + INVITE_COOLDOWN)
            if verdict is not ManageVerdict.OK:
                return InviteSendResult(verdict)
            if member is None:
                return InviteSendResult(ManageVerdict.MISSING)
            title = row.title
            owner_id = int(row.owner_id)
            if existing_row is None:
                session.add(
                    GroupInvite(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        invited_by=int(actor_id),
                        created_at=moment,
                        expires_at=moment + INVITE_TTL,
                        status=InviteStatus.PENDING.value,
                        dm_sent=False,
                    )
                )
            else:
                existing_row.invited_by = int(actor_id)
                existing_row.created_at = moment
                existing_row.expires_at = moment + INVITE_TTL
                existing_row.status = InviteStatus.PENDING.value
                existing_row.dm_sent = False
                existing_row.decided_at = None
        sent, closed = await self._deliver_invites(guild, int(group_id), title, owner_id, (int(user_id),))
        return InviteSendResult(ManageVerdict.OK, dm_sent=sent > 0 and closed == 0)

    async def accept_invite(
        self,
        guild: discord.Guild,
        group_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            invite = await session.get(GroupInvite, (int(group_id), int(actor_id)))
            row = await session.get(PrivateGroup, int(group_id))
            if invite is None or row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if group_state(archived_at=row.archived_at) is GroupState.ARCHIVED:
                return ManageVerdict.EXPIRED
            if invite.status != InviteStatus.PENDING.value or as_utc(invite.expires_at) <= moment:
                invite.status = InviteStatus.EXPIRED.value
                invite.decided_at = moment
                return ManageVerdict.EXPIRED
            members = await self._member_ids(session, row.id)
            verdict = invite_verdict(user_id=int(actor_id), member_ids=members, is_bot=False)
            if verdict is not ManageVerdict.OK:
                return verdict
            invite.status = InviteStatus.ACCEPTED.value
            invite.decided_at = moment
            session.add(GroupMember(group_id=int(row.id), user_id=int(actor_id)))
            text_id = int(row.channel_id)
            voice_id = None if row.voice_channel_id is None else int(row.voice_channel_id)
        target = guild.get_member(int(actor_id))
        if target is not None:
            await self._grant(guild, text_id, voice_id, target)
        channel = guild.get_channel(text_id)
        if channel is not None:
            try:
                await self._discord(
                    send_card(channel, panel(copy.GROUP_TITLE, copy.group_joined_line(actor_id), tone=Tone.OK))
                )
            except discord.HTTPException as exc:
                log.warning("group_join_notice_failed", group_id=group_id, error=str(exc))
        await self._refresh(guild, int(group_id))
        return ManageVerdict.OK

    async def decline_invite(
        self,
        guild: discord.Guild,
        group_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> ManageVerdict:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            invite = await session.get(GroupInvite, (int(group_id), int(actor_id)))
            row = await session.get(PrivateGroup, int(group_id))
            if invite is None or row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if invite.status != InviteStatus.PENDING.value or as_utc(invite.expires_at) <= moment:
                invite.status = InviteStatus.EXPIRED.value
                invite.decided_at = moment
                return ManageVerdict.EXPIRED
            invite.status = InviteStatus.DECLINED.value
            invite.decided_at = moment
        return ManageVerdict.OK

    async def invite_guard(self, guild_id: int, ref: int | str, *, user_id: int) -> GuardResource | None:
        try:
            group_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            invite = await session.get(GroupInvite, (group_id, int(user_id)))
            if invite is None:
                return None
            row = await session.get(PrivateGroup, group_id)
            if row is None:
                return None
            if int(guild_id) and int(row.guild_id) != int(guild_id):
                return None
            return GuardResource(
                ref=str(row.id),
                guild_id=int(row.guild_id),
                owner_id=int(user_id),
                closed=False,
                channel_id=int(row.channel_id),
            )

    async def channel_id_of(self, guild_id: int, group_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or int(row.guild_id) != int(guild_id):
                return None
            return int(row.channel_id)

    async def mine(
        self,
        guild: discord.Guild,
        actor: Any,
        *,
        now: datetime | None = None,
    ) -> GroupListing:
        moment = as_utc(now or datetime.now(UTC))
        actor_id = int(actor.id)
        _has_key, privileged, settings, _score, created_at, open_count = await self._create_gate(
            guild, actor, moment
        )
        quota = group_quota(
            created_at=created_at,
            open_count=open_count,
            now=moment,
            daily_limit=settings.daily_limit,
            open_limit=settings.open_limit,
            privileged=privileged,
        )
        async with session_scope(self._bot.sessions) as session:
            owned = (
                await session.scalars(
                    select(PrivateGroup).where(
                        PrivateGroup.guild_id == guild.id,
                        PrivateGroup.owner_id == actor_id,
                        PrivateGroup.archived_at.is_(None),
                    )
                )
            ).all()
            member_rows = (
                await session.scalars(select(GroupMember).where(GroupMember.user_id == actor_id))
            ).all()
            member_ids = {int(item.group_id) for item in member_rows}
            extra = []
            if member_ids:
                extra = (
                    await session.scalars(
                        select(PrivateGroup).where(
                            PrivateGroup.guild_id == guild.id,
                            PrivateGroup.id.in_(member_ids),
                            PrivateGroup.archived_at.is_(None),
                        )
                    )
                ).all()
            seen: set[int] = set()
            cards: list[GroupCard] = []
            for row in (*owned, *extra):
                if int(row.id) in seen:
                    continue
                seen.add(int(row.id))
                cards.append(await self._card(session, row))
            pending = (
                await session.scalars(
                    select(GroupInvite).where(
                        GroupInvite.user_id == actor_id,
                        GroupInvite.status == InviteStatus.PENDING.value,
                        GroupInvite.expires_at > moment,
                    )
                )
            ).all()
            invites: list[GroupInviteCard] = []
            for invite in pending:
                row = await session.get(PrivateGroup, int(invite.group_id))
                if row is None or int(row.guild_id) != int(guild.id) or row.archived_at is not None:
                    continue
                invites.append(
                    GroupInviteCard(
                        group_id=int(row.id),
                        title=row.title,
                        owner_id=int(row.owner_id),
                        guild_id=int(row.guild_id),
                        channel_id=int(row.channel_id),
                        dm_sent=bool(invite.dm_sent),
                        expires_at=as_utc(invite.expires_at),
                    )
                )
        return GroupListing(
            groups=tuple(cards),
            invites=tuple(invites),
            open_count=open_count,
            open_limit=settings.open_limit,
            retry_at=None if quota.ok else quota.retry_at,
            privileged=privileged,
            viewer_id=actor_id,
        )

    async def leave(
        self, guild: discord.Guild, group_id: int, *, actor_id: int, now: datetime | None = None
    ) -> ManageVerdict | tuple[ManageVerdict, Teardown | None]:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING
            if group_state(archived_at=row.archived_at) is GroupState.ARCHIVED:
                return ManageVerdict.MISSING
            members = await self._member_ids(session, row.id)
            verdict = can_leave(actor_id=actor_id, owner_id=row.owner_id, member_ids=members)
            if verdict is ManageVerdict.OWNER:
                return await self.archive(
                    guild, int(group_id), actor_id=actor_id, reason=ArchiveReason.OWNER_LEFT, now=now
                )
            if verdict is not ManageVerdict.OK:
                return verdict
            text_id = int(row.channel_id)
            voice_id = None if row.voice_channel_id is None else int(row.voice_channel_id)
        target = guild.get_member(int(actor_id)) or discord.Object(id=int(actor_id))
        await self._revoke(guild, text_id, voice_id, target)
        async with session_scope(self._bot.sessions) as session:
            member_row = await session.scalar(
                select(GroupMember).where(GroupMember.group_id == int(group_id), GroupMember.user_id == int(actor_id))
            )
            if member_row is not None:
                await session.delete(member_row)
        await self._refresh(guild, int(group_id))
        return ManageVerdict.OK

    async def archive(
        self,
        guild: discord.Guild,
        group_id: int,
        *,
        actor_id: int,
        reason: ArchiveReason,
        now: datetime | None = None,
    ) -> tuple[ManageVerdict, Teardown | None]:
        moment = as_utc(now or datetime.now(UTC))
        is_staff = await self._actor_is_staff(guild, actor_id)
        system = int(actor_id) == SYSTEM_ACTOR and reason is ArchiveReason.SILENCE
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING, None
            if group_state(archived_at=row.archived_at) is GroupState.ARCHIVED:
                return ManageVerdict.ALREADY, None
            if not system and not can_archive(actor_id=actor_id, owner_id=row.owner_id, is_staff=is_staff):
                return ManageVerdict.DENIED, None
            text_id = int(row.channel_id)
            voice_id = None if row.voice_channel_id is None else int(row.voice_channel_id)
            title = row.title
            owner_id = int(row.owner_id)
            members = await self._member_ids(session, row.id)
            created_at = as_utc(row.created_at)
            archive_days = await self._archive_days_for(session, guild.id)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None:
                return ManageVerdict.MISSING, None
            if row.archived_at is not None:
                return ManageVerdict.ALREADY, None
            row.archived_at = moment
            row.archived_by = None if system else int(actor_id)
            row.archive_reason = reason.value
            row.purge_after = purge_at(moment, archive_days=archive_days)
            row.silence_warned_at = None
            await session.execute(
                update(GroupInvite)
                .where(GroupInvite.group_id == int(group_id), GroupInvite.status == InviteStatus.PENDING.value)
                .values(status=InviteStatus.EXPIRED.value, decided_at=moment)
            )
            await mark_stale_by_ref(session, guild_id=guild.id, kind=PANEL_KIND, ref=group_id)
        self._cancel_silence(int(group_id))
        self._cancel_warn(int(group_id))
        self._activity_written.pop(int(group_id), None)
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _archive_overwrites(guild, snapshot)
        card = GroupCard(
            group_id=int(group_id),
            title=title,
            owner_id=owner_id,
            member_ids=members,
            channel_id=text_id,
            voice_channel_id=voice_id,
            state=GroupState.ARCHIVED,
            created_at=created_at,
            archived_at=moment,
            archive_reason=reason,
            purge_after=purge_at(moment, archive_days=archive_days),
        )

        async def teardown() -> None:
            shelf = await self._pick_shelf(guild)
            text = guild.get_channel(text_id)
            topic = copy.group_archive_topic(title, owner_id, moment, reason.value)
            if text is not None:
                await self._move_channel(
                    text, category=shelf, overwrites=overwrites, topic=topic, reason=_ARCHIVE_REASON
                )
                try:
                    header = archive_header_view(card)
                    await self._discord(send_card(text, header, mentions=discord.AllowedMentions.none()))
                except discord.HTTPException as exc:
                    log.warning("group_archive_header_failed", group_id=group_id, error=str(exc))
            if voice_id is not None:
                voice = guild.get_channel(voice_id)
                if voice is None:
                    pass
                elif await _voice_has_messages(voice):
                    await self._move_channel(
                        voice, category=shelf, overwrites=overwrites, topic=None, reason=_ARCHIVE_REASON
                    )
                else:
                    await self._safe_delete(voice)
            await self._notify_archived(guild, members, title, reason, archive_days)

        return ManageVerdict.OK, teardown

    async def purge(
        self, guild: discord.Guild, group_id: int, *, actor_id: int, now: datetime | None = None
    ) -> tuple[ManageVerdict, Teardown | None]:
        is_staff = await self._actor_is_staff(guild, actor_id)
        system = int(actor_id) == SYSTEM_ACTOR
        if not system and not can_purge(is_staff=is_staff):
            return ManageVerdict.DENIED, None
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING, None
            if group_state(archived_at=row.archived_at) is not GroupState.ARCHIVED:
                return ManageVerdict.DENIED, None
            text_id = int(row.channel_id)
            voice_id = None if row.voice_channel_id is None else int(row.voice_channel_id)
            title = row.title
            owner_id = int(row.owner_id)
            await session.execute(delete(GroupInvite).where(GroupInvite.group_id == int(group_id)))
            await session.execute(delete(GroupMember).where(GroupMember.group_id == int(group_id)))
            await session.delete(row)
        snapshot = await cached_snapshot(self._bot, guild.id)
        if not system:
            member = guild.get_member(int(actor_id))
            role_ids = tuple(int(role.id) for role in getattr(member, "roles", ()) or ())
            rank = resolve_rank(role_ids, snapshot.rank_roles)
            await emit_staff(
                self._bot,
                guild.id,
                actor_id=int(actor_id),
                actor_rank=rank,
                action=ACTION_GROUP_PURGE,
                ref=int(group_id),
                details=f"{title} #{int(group_id)}",
                now=moment,
            )

        async def teardown() -> None:
            await self._safe_delete(guild.get_channel(text_id), reason=_PURGE_REASON)
            if voice_id is not None:
                await self._safe_delete(guild.get_channel(voice_id), reason=_PURGE_REASON)

        del owner_id
        return ManageVerdict.OK, teardown

    async def restore_from_archive(
        self, guild: discord.Guild, group_id: int, *, actor_id: int, now: datetime | None = None
    ) -> tuple[ManageVerdict, Teardown | None]:
        is_staff = await self._actor_is_staff(guild, actor_id)
        if not can_restore(is_staff=is_staff):
            return ManageVerdict.DENIED, None
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild.id:
                return ManageVerdict.MISSING, None
            if group_state(archived_at=row.archived_at) is not GroupState.ARCHIVED:
                return ManageVerdict.DENIED, None
            text_id = int(row.channel_id)
            voice_id = None if row.voice_channel_id is None else int(row.voice_channel_id)
            members = await self._member_ids(session, row.id)
            row.archived_at = None
            row.archived_by = None
            row.archive_reason = None
            row.purge_after = None
            row.silence_warned_at = None
            row.last_activity_at = moment
            silence = await self._silence_for(session, guild.id)
        self._schedule_silence(int(group_id), seconds_until_silence(moment, moment, silence_after=silence))
        self._schedule_warn(int(group_id), seconds_until_silence_warn(moment, moment, silence_after=silence))
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _overwrites(guild, members, snapshot)
        live = await self._ensure_category(guild)

        async def teardown() -> None:
            text = guild.get_channel(text_id)
            if text is not None:
                await self._move_channel(text, category=live, overwrites=overwrites, topic="", reason=_RESTORE_REASON)
            if voice_id is not None:
                voice = guild.get_channel(voice_id)
                if voice is not None:
                    await self._move_channel(
                        voice, category=live, overwrites=overwrites, topic=None, reason=_RESTORE_REASON
                    )
            await self._refresh(guild, int(group_id))

        return ManageVerdict.OK, teardown

    async def touch_activity(self, guild_id: int, channel_id: int, *, now: datetime | None = None) -> None:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(PrivateGroup).where(
                    PrivateGroup.guild_id == guild_id,
                    PrivateGroup.channel_id == channel_id,
                    PrivateGroup.archived_at.is_(None),
                )
            )
            if row is None:
                return
            group_id = int(row.id)
            if not activity_write_due(self._activity_written.get(group_id), moment):
                return
            row.last_activity_at = moment
            row.silence_warned_at = None
            self._activity_written[group_id] = moment
            silence = await self._silence_for(session, guild_id)
        self._schedule_silence(group_id, seconds_until_silence(moment, moment, silence_after=silence))
        self._schedule_warn(group_id, seconds_until_silence_warn(moment, moment, silence_after=silence))

    async def restore(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(PrivateGroup).where(PrivateGroup.archived_at.is_(None)))).all()
            pending: list[tuple[int, datetime, int]] = []
            silences: dict[int, timedelta] = {}
            for row in rows:
                if row.guild_id not in silences:
                    silences[row.guild_id] = await self._silence_for(session, row.guild_id)
                pending.append((int(row.id), as_utc(row.last_activity_at), row.guild_id))
        for group_id, last, guild_id in pending:
            if not guild_is_active(self._bot, int(guild_id)):
                continue
            delay = seconds_until_silence(last, moment, silence_after=silences[guild_id])
            self._schedule_silence(group_id, delay)
            warn = seconds_until_silence_warn(last, moment, silence_after=silences[guild_id])
            self._schedule_warn(group_id, warn)
        return len(pending)

    async def sweep_hourly(self, *, now: datetime | None = None, guild_id: int | None = None) -> None:
        moment = as_utc(now or datetime.now(UTC))
        warned = await self.sweep_warnings(now=moment, guild_id=guild_id)
        archived = await self.sweep_silence(now=moment, guild_id=guild_id)
        purged = await self.sweep_purge(now=moment, guild_id=guild_id)
        expired = await self.expire_invites(now=moment)
        log_tick(log, "groups_sweep", changed=warned + archived + purged + expired)

    async def sweep_silence(self, *, now: datetime | None = None, guild_id: int | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            query = select(PrivateGroup).where(PrivateGroup.archived_at.is_(None))
            if guild_id is not None:
                query = query.where(PrivateGroup.guild_id == int(guild_id))
            rows = (await session.scalars(query)).all()
            silences: dict[int, timedelta] = {}
            pending: list[tuple[int, int, datetime]] = []
            for row in rows:
                if row.guild_id not in silences:
                    silences[int(row.guild_id)] = await self._silence_for(session, int(row.guild_id))
                pending.append((int(row.id), int(row.guild_id), as_utc(row.last_activity_at)))
        archived = 0
        for group_id, gid, last in pending:
            if not silence_due(last, moment, silence_after=silences[gid]):
                continue
            guild = self._bot.get_guild(gid)
            if guild is None:
                continue
            _verdict, teardown = await self.archive(
                guild, group_id, actor_id=SYSTEM_ACTOR, reason=ArchiveReason.SILENCE, now=moment
            )
            if teardown is None:
                continue
            try:
                await teardown()
            except Exception:
                log.exception("group_silence_teardown_failed", group_id=group_id)
                continue
            archived += 1
        return archived

    async def sweep_purge(self, *, now: datetime | None = None, guild_id: int | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            query = select(PrivateGroup).where(
                PrivateGroup.archived_at.is_not(None),
                PrivateGroup.purge_after.is_not(None),
                PrivateGroup.purge_after < moment,
            )
            if guild_id is not None:
                query = query.where(PrivateGroup.guild_id == int(guild_id))
            rows = (await session.scalars(query)).all()
            pending = [(int(row.id), int(row.guild_id)) for row in rows]
        purged = 0
        for group_id, gid in pending:
            guild = self._bot.get_guild(gid)
            if guild is None:
                continue
            _verdict, teardown = await self.purge(guild, group_id, actor_id=SYSTEM_ACTOR, now=moment)
            if teardown is None:
                continue
            try:
                await teardown()
            except Exception:
                log.exception("group_purge_teardown_failed", group_id=group_id)
                continue
            purged += 1
        return purged

    async def sweep_warnings(self, *, now: datetime | None = None, guild_id: int | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            query = select(PrivateGroup).where(
                PrivateGroup.archived_at.is_(None),
                PrivateGroup.silence_warned_at.is_(None),
            )
            if guild_id is not None:
                query = query.where(PrivateGroup.guild_id == int(guild_id))
            rows = (await session.scalars(query)).all()
            silences: dict[int, timedelta] = {}
            pending: list[tuple[int, int, int, datetime]] = []
            for row in rows:
                if row.guild_id not in silences:
                    silences[int(row.guild_id)] = await self._silence_for(session, int(row.guild_id))
                pending.append((int(row.id), int(row.guild_id), int(row.channel_id), as_utc(row.last_activity_at)))
        warned = 0
        for group_id, gid, channel_id, last in pending:
            silence = silences[gid]
            if silence_due(last, moment, silence_after=silence):
                continue
            if not silence_warn_due(last, moment, silence_after=silence):
                continue
            if await self._post_silence_warning(gid, group_id, channel_id, last, silence, moment):
                warned += 1
        return warned

    async def expire_invites(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(GroupInvite)
                .where(GroupInvite.status == InviteStatus.PENDING.value, GroupInvite.expires_at < moment)
                .values(status=InviteStatus.EXPIRED.value, decided_at=moment)
            )
        return int(result.rowcount or 0)

    async def hub_settings(self, guild_id: int) -> GroupHubSettings | None:
        async with session_scope(self._bot.sessions) as session:
            hub = await session.get(GroupHub, int(guild_id))
            if hub is None:
                return None
            return _hub_settings(hub)

    async def set_numbers(self, guild_id: int, silence_days: int, archive_days: int) -> None:
        days = max(1, int(silence_days))
        keep = max(0, int(archive_days))
        async with session_scope(self._bot.sessions) as session:
            hub = await self._hub_row(session, int(guild_id))
            hub.silence_seconds = days * 86400
            hub.archive_days = keep

    async def set_limits(self, guild_id: int, min_reputation: int, daily_limit: int, open_limit: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            hub = await self._hub_row(session, int(guild_id))
            hub.min_reputation = max(0, int(min_reputation))
            hub.daily_limit = max(0, int(daily_limit))
            hub.open_limit = max(0, int(open_limit))

    async def bind_archive_category(self, guild_id: int, category_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            hub = await self._hub_row(session, int(guild_id))
            before = None if hub.archive_category_id is None else int(hub.archive_category_id)
            hub.archive_category_id = int(category_id)
        return before

    async def group_by_channel(self, guild_id: int, channel_id: int) -> GroupCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(PrivateGroup).where(PrivateGroup.guild_id == guild_id, PrivateGroup.channel_id == channel_id)
            )
            if row is None:
                return None
            return await self._card(session, row)

    async def card(self, guild_id: int, group_id: int) -> GroupCard | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.guild_id != guild_id:
                return None
            return await self._card(session, row)

    async def guard_resource(self, guild_id: int, ref: int | str, *, need: str = "open") -> GuardResource | None:
        try:
            group_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, group_id)
            if row is None or row.guild_id != guild_id:
                return None
            state = group_state(archived_at=row.archived_at)
            closed = False
            if need == "open":
                closed = state is not GroupState.OPEN
            elif need == "archived":
                closed = state is not GroupState.ARCHIVED
            return GuardResource(
                ref=str(row.id),
                guild_id=row.guild_id,
                owner_id=row.owner_id,
                closed=closed,
                channel_id=row.channel_id,
            )

    async def _silence_archive(self, group_id: int, *, now: datetime | None = None) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.archived_at is not None:
                return
            guild_id = row.guild_id
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        _verdict, teardown = await self.archive(
            guild, int(group_id), actor_id=SYSTEM_ACTOR, reason=ArchiveReason.SILENCE, now=now
        )
        if teardown is None:
            return
        try:
            await teardown()
        except Exception:
            log.exception("group_archive_teardown_failed", group_id=group_id)

    async def _warn_silence(self, group_id: int, *, now: datetime | None = None) -> None:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, int(group_id))
            if row is None or row.archived_at is not None or row.silence_warned_at is not None:
                return
            silence = await self._silence_for(session, int(row.guild_id))
            last = as_utc(row.last_activity_at)
            guild_id = int(row.guild_id)
            channel_id = int(row.channel_id)
        if silence_due(last, moment, silence_after=silence):
            return
        if not silence_warn_due(last, moment, silence_after=silence):
            return
        await self._post_silence_warning(guild_id, int(group_id), channel_id, last, silence, moment)

    async def _post_silence_warning(
        self,
        guild_id: int,
        group_id: int,
        channel_id: int,
        last: datetime,
        silence: timedelta,
        moment: datetime,
    ) -> bool:
        guild = self._bot.get_guild(guild_id)
        channel = None if guild is None else guild.get_channel(channel_id)
        if channel is None:
            return False
        deadline = silence_deadline(last, silence_after=silence)
        view = panel(copy.GROUP_TITLE, copy.group_silence_warn(deadline), tone=Tone.WARN)
        try:
            await self._discord(send_card(channel, view, mentions=discord.AllowedMentions.none()))
        except discord.HTTPException as exc:
            log.warning("group_silence_warn_failed", group_id=group_id, error=str(exc))
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(PrivateGroup, group_id)
            if row is None or row.archived_at is not None or row.silence_warned_at is not None:
                return False
            row.silence_warned_at = moment
        return True

    async def _ensure_category(self, guild: discord.Guild) -> Any | None:
        async with session_scope(self._bot.sessions) as session:
            hub = await session.get(GroupHub, guild.id)
            category_id = None if hub is None else int(hub.category_id)
        found = None if category_id is None else _as_category(guild.get_channel(category_id))
        if found is not None:
            return found
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _staff_overwrites(guild, snapshot)
        created = await self._create_category(guild, HUB_NAME, overwrites, reason=_CREATE_REASON)
        if created is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            hub = await session.get(GroupHub, guild.id)
            if hub is None:
                session.add(GroupHub(guild_id=guild.id, category_id=int(created.id)))
            else:
                hub.category_id = int(created.id)
        return created

    async def _ensure_archive_category(self, guild: discord.Guild) -> Any | None:
        async with session_scope(self._bot.sessions) as session:
            hub = await session.get(GroupHub, guild.id)
            category_id = None if hub is None or hub.archive_category_id is None else int(hub.archive_category_id)
        found = None if category_id is None else _as_category(guild.get_channel(category_id))
        if found is not None:
            return found
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _archive_overwrites(guild, snapshot)
        created = await self._create_category(guild, LAYOUT.cat_group_archive, overwrites, reason=_ARCHIVE_REASON)
        if created is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            hub = await self._hub_row(session, guild.id)
            hub.archive_category_id = int(created.id)
        return created

    async def _pick_shelf(self, guild: discord.Guild) -> Any | None:
        primary = await self._ensure_archive_category(guild)
        if primary is not None and not _category_is_full(primary):
            return primary
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(select(GroupArchiveShelf).where(GroupArchiveShelf.guild_id == guild.id))
            ).all()
            extra_ids = [int(row.category_id) for row in rows]
        for extra_id in extra_ids:
            extra = _as_category(guild.get_channel(extra_id))
            if extra is not None and not _category_is_full(extra):
                return extra
        if _guild_is_full(guild):
            log.warning("group_archive_guild_full", guild_id=guild.id)
            self._post_structure(guild, panel(copy.GROUP_TITLE, copy.HOLD_CAP_CHANNEL, tone=Tone.WARN))
            return primary
        name = str(getattr(primary, "name", "") or LAYOUT.cat_group_archive)
        overwrites = dict(getattr(primary, "overwrites", {}) or {}) if primary is not None else _archive_overwrites(
            guild, await cached_snapshot(self._bot, guild.id)
        )
        created = await self._create_category(guild, name, overwrites, reason=_ARCHIVE_REASON)
        if created is None:
            return primary
        async with session_scope(self._bot.sessions) as session:
            session.add(
                GroupArchiveShelf(category_id=int(created.id), guild_id=guild.id, created_at=datetime.now(UTC))
            )
        return created

    async def _create_category(
        self, guild: discord.Guild, name: str, overwrites: dict[Any, Any], *, reason: str
    ) -> Any | None:
        if _guild_is_full(guild):
            log.warning("group_category_guild_full", guild_id=guild.id)
            self._post_structure(guild, panel(copy.GROUP_TITLE, copy.HOLD_CAP_CHANNEL, tone=Tone.WARN))
            return None
        hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
        if hold is not None:
            log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            return None
        try:
            with lane(Lane.USER):
                created = await self._discord(guild.create_category(name, overwrites=overwrites, reason=reason))
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            log.warning("group_category_failed", guild_id=guild.id, error=str(exc))
            if getattr(exc, "code", None) == 30013:
                self._post_structure(guild, panel(copy.GROUP_TITLE, copy.HOLD_CAP_CHANNEL, tone=Tone.WARN))
            return None
        note_success(self._bot, guild.id, BUCKET_CHANNEL)
        return created

    async def _move_channel(
        self,
        channel: Any,
        *,
        category: Any | None,
        overwrites: dict[Any, Any],
        topic: str | None,
        reason: str,
    ) -> None:
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            return
        kwargs: dict[str, Any] = {
            "category": category,
            "sync_permissions": False,
            "overwrites": overwrites,
            "reason": reason,
        }
        if topic is not None and getattr(channel, "type", None) is not discord.ChannelType.voice:
            kwargs["topic"] = topic
        try:
            with lane(Lane.USER):
                await self._discord(edit(**kwargs))
        except (discord.HTTPException, discord.RateLimited) as exc:
            log.warning("group_move_failed", channel_id=getattr(channel, "id", None), error=str(exc))

    async def _notify_archived(
        self,
        guild: discord.Guild,
        member_ids: tuple[int, ...],
        title: str,
        reason: ArchiveReason,
        archive_days: int,
    ) -> None:
        view = panel(copy.GROUP_TITLE, copy.group_archive_dm(title, reason.value, archive_days), tone=Tone.MUTED)
        with lane(Lane.BACKGROUND):
            for user_id in member_ids:
                member = guild.get_member(int(user_id))
                if member is None:
                    continue
                try:
                    await self._discord(send_card(member, view, mentions=discord.AllowedMentions.none()))
                except discord.Forbidden:
                    continue
                except discord.HTTPException as exc:
                    log.warning("group_archive_dm_failed", user_id=user_id, error=str(exc))

    def _post_structure(self, guild: discord.Guild, view: object) -> None:
        submit = getattr(getattr(self._bot, "log_pump", None), "submit", None)
        if not callable(submit):
            return
        configs = getattr(self._bot, "config_by_guild", None)
        config = configs.get(int(guild.id)) if configs is not None else None
        channel_id = None if config is None else (config.structure_channel_id or config.admin_log_channel_id)
        if channel_id:
            submit(int(channel_id), view=view)

    async def _actor_is_staff(self, guild: discord.Guild, actor_id: int) -> bool:
        if int(actor_id) == SYSTEM_ACTOR:
            return False
        snapshot = await cached_snapshot(self._bot, guild.id)
        member = guild.get_member(int(actor_id))
        role_ids = tuple(int(role.id) for role in getattr(member, "roles", ()) or ())
        is_admin = bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))
        probe = getattr(self._bot, "is_developer", None)
        is_dev = bool(probe(actor_id)) if callable(probe) else False
        decision = decide(
            snapshot,
            key=GROUP_ARCHIVE_MANAGE,
            member_role_ids=role_ids,
            is_administrator=is_admin,
            is_developer=is_dev,
        )
        if decision.allowed or decision.developer:
            return True
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        return rank in STAFF_RANKS

    async def _refresh(self, guild: discord.Guild, group_id: int) -> None:
        card = await self.card(guild.id, group_id)
        if card is None or card.state is GroupState.ARCHIVED:
            return
        message = self._locate(guild, card.channel_id, card.message_id)
        if message is None:
            return
        try:
            await self._discord(edit_card(message, header_view(card)))
        except discord.HTTPException as exc:
            log.warning("group_header_edit_failed", group_id=group_id, error=str(exc))

    async def _grant(self, guild: discord.Guild, text_id: int, voice_id: int | None, member: Any) -> None:
        overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            connect=True,
            speak=True,
        )
        for channel_id in (text_id, voice_id):
            if channel_id is None:
                continue
            channel = guild.get_channel(channel_id)
            setter = getattr(channel, "set_permissions", None)
            if not callable(setter):
                continue
            try:
                await self._discord(setter(member, overwrite=overwrite, reason=_CREATE_REASON))
            except discord.HTTPException as exc:
                log.warning("group_grant_failed", channel_id=channel_id, error=str(exc))

    async def _revoke(self, guild: discord.Guild, text_id: int, voice_id: int | None, member: Any) -> None:
        for channel_id in (text_id, voice_id):
            if channel_id is None:
                continue
            channel = guild.get_channel(channel_id)
            setter = getattr(channel, "set_permissions", None)
            if not callable(setter):
                continue
            try:
                await self._discord(setter(member, overwrite=None, reason=_ARCHIVE_REASON))
            except discord.HTTPException as exc:
                log.warning("group_revoke_failed", channel_id=channel_id, error=str(exc))

    async def _card(self, session: AsyncSession, row: PrivateGroup) -> GroupCard:
        reason = None
        if row.archive_reason:
            try:
                reason = ArchiveReason(row.archive_reason)
            except ValueError:
                reason = None
        return GroupCard(
            group_id=int(row.id),
            title=row.title,
            owner_id=int(row.owner_id),
            member_ids=await self._member_ids(session, int(row.id)),
            channel_id=int(row.channel_id),
            voice_channel_id=None if row.voice_channel_id is None else int(row.voice_channel_id),
            message_id=None if row.message_id is None else int(row.message_id),
            state=group_state(archived_at=row.archived_at),
            created_at=as_utc(row.created_at),
            last_activity_at=as_utc(row.last_activity_at),
            archived_at=None if row.archived_at is None else as_utc(row.archived_at),
            archive_reason=reason,
            purge_after=None if row.purge_after is None else as_utc(row.purge_after),
            silence_after=await self._silence_for(session, row.guild_id),
        )

    async def _member_ids(self, session: AsyncSession, group_id: int) -> tuple[int, ...]:
        rows = (await session.scalars(select(GroupMember).where(GroupMember.group_id == group_id))).all()
        return tuple(int(row.user_id) for row in rows)

    async def _pending_ids(self, session: AsyncSession, group_id: int) -> tuple[int, ...]:
        rows = (
            await session.scalars(
                select(GroupInvite).where(
                    GroupInvite.group_id == int(group_id),
                    GroupInvite.status == InviteStatus.PENDING.value,
                )
            )
        ).all()
        return tuple(int(row.user_id) for row in rows)

    async def _create_gate(
        self, guild: discord.Guild, actor: Any, moment: datetime
    ) -> tuple[bool, bool, GroupHubSettings, int, tuple[datetime, ...], int]:
        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(int(role.id) for role in getattr(actor, "roles", ()) or ())
        is_admin = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))
        owner_id = int(getattr(guild, "owner_id", 0) or 0)
        is_owner = owner_id == int(actor.id)
        probe = getattr(self._bot, "is_developer", None)
        is_dev = bool(probe(int(actor.id))) if callable(probe) else False
        decision = decide(
            snapshot,
            key=GROUP_CREATE,
            member_role_ids=role_ids,
            is_administrator=is_admin,
            is_developer=is_dev,
        )
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        privileged = is_special_or_admin(
            rank=rank, is_administrator=is_admin, is_guild_owner=is_owner
        )
        has_key = bool(decision.allowed or decision.developer)
        async with session_scope(self._bot.sessions) as session:
            hub = await session.get(GroupHub, int(guild.id))
            settings = (
                _hub_settings(hub)
                if hub is not None
                else GroupHubSettings(category_id=0)
            )
            score_row = await session.scalar(
                select(Reputation).where(
                    Reputation.guild_id == int(guild.id), Reputation.user_id == int(actor.id)
                )
            )
            score = START if score_row is None else int(score_row.score)
            owned = (
                await session.scalars(
                    select(PrivateGroup).where(
                        PrivateGroup.guild_id == int(guild.id), PrivateGroup.owner_id == int(actor.id)
                    )
                )
            ).all()
            created_at = tuple(as_utc(row.created_at) for row in owned)
            open_count = sum(1 for row in owned if row.archived_at is None)
        _ = moment
        return has_key, privileged, settings, score, created_at, open_count

    async def _deliver_invites(
        self,
        guild: discord.Guild,
        group_id: int,
        title: str,
        owner_id: int,
        user_ids: tuple[int, ...],
    ) -> tuple[int, int]:
        sent = 0
        closed = 0
        guild_url = _guild_url(guild)
        guild_name = str(getattr(guild, "name", "") or guild.id)
        view = invite_dm_view(int(group_id), int(owner_id), title, guild_name, guild_url)
        for user_id in user_ids:
            member = guild.get_member(int(user_id))
            delivered = False
            if member is not None and not getattr(member, "bot", False):
                try:
                    await self._discord(send_card(member, view, mentions=discord.AllowedMentions.none()))
                    delivered = True
                except discord.Forbidden:
                    delivered = False
                except discord.HTTPException as exc:
                    log.warning("group_invite_dm_failed", user_id=user_id, error=str(exc))
                    delivered = False
            if delivered:
                sent += 1
            else:
                closed += 1
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(GroupInvite, (int(group_id), int(user_id)))
                if row is not None:
                    row.dm_sent = delivered
        return sent, closed

    async def _silence_for(self, session: AsyncSession, guild_id: int) -> timedelta:
        hub = await session.get(GroupHub, guild_id)
        if hub is None:
            return DEFAULT_SILENCE
        return timedelta(seconds=max(60, int(hub.silence_seconds)))

    async def _archive_days_for(self, session: AsyncSession, guild_id: int) -> int:
        hub = await session.get(GroupHub, guild_id)
        if hub is None:
            return DEFAULT_ARCHIVE_DAYS
        return max(0, int(hub.archive_days))

    async def _hub_row(self, session: AsyncSession, guild_id: int) -> GroupHub:
        hub = await session.get(GroupHub, int(guild_id))
        if hub is not None:
            return hub
        hub = GroupHub(guild_id=int(guild_id), category_id=0)
        session.add(hub)
        await session.flush()
        return hub

    def _schedule_silence(self, group_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("gr-silence", group_id), delay, lambda gid=group_id: self._silence_archive(gid))

    def _cancel_silence(self, group_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if callable(cancel):
            cancel(("gr-silence", group_id))

    def _schedule_warn(self, group_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return
        schedule(("gr-warn", group_id), delay, lambda gid=group_id: self._warn_silence(gid))

    def _cancel_warn(self, group_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if callable(cancel):
            cancel(("gr-warn", group_id))

    def _locate(self, guild: discord.Guild, channel_id: int, message_id: int | None) -> Any | None:
        if message_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            return None
        partial = getattr(channel, "get_partial_message", None)
        if callable(partial):
            return partial(message_id)
        messages = getattr(channel, "messages", None)
        if isinstance(messages, dict):
            return messages.get(message_id)
        return None

    async def _safe_delete(self, channel: Any, *, reason: str = _ARCHIVE_REASON) -> None:
        delete = getattr(channel, "delete", None)
        if not callable(delete):
            return
        guild = getattr(channel, "guild", None)
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id:
            hold = await hold_for(self._bot, guild_id, BUCKET_DELETE)
            if hold is not None:
                log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
                return
        try:
            await self._discord(delete(reason=reason))
        except (discord.HTTPException, discord.RateLimited) as exc:
            if guild_id:
                hold = trip(self._bot, guild_id, BUCKET_DELETE, exc)
                if hold is not None:
                    log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
            log.warning("group_delete_failed", error=str(exc))
            return
        if guild_id:
            note_success(self._bot, guild_id, BUCKET_DELETE)

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _guild_url(guild: discord.Guild) -> str:
    vanity = getattr(guild, "vanity_url_code", None)
    if vanity:
        return f"https://discord.gg/{vanity}"
    return f"https://discord.com/channels/{int(guild.id)}"


def _as_category(channel: Any) -> Any | None:
    if channel is None:
        return None
    if isinstance(channel, discord.CategoryChannel):
        return channel
    if getattr(channel, "type", None) is discord.ChannelType.category:
        return channel
    return None


def _category_is_full(category: Any) -> bool:
    children = getattr(category, "channels", None)
    try:
        count = len(children) if children is not None else 0
    except TypeError:
        return False
    return count >= CATEGORY_CHILDREN


def _guild_is_full(guild: object) -> bool:
    channels = getattr(guild, "channels", None)
    try:
        count = len(channels) if channels is not None else 0
    except TypeError:
        parts = (
            getattr(guild, "voice_channels", ()) or (),
            getattr(guild, "text_channels", ()) or (),
            getattr(guild, "categories", ()) or (),
        )
        try:
            count = sum(len(part) for part in parts)
        except TypeError:
            return False
    return count >= GUILD_CHANNELS


def _member_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        connect=True,
        speak=True,
    )


def _bot_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        manage_channels=True,
        manage_messages=True,
        connect=True,
        speak=True,
        move_members=True,
    )


def _archive_reader_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=False,
        add_reactions=False,
        connect=False,
        speak=False,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )


def _staff_overwrites(guild: discord.Guild, snapshot: Any) -> dict[Any, discord.PermissionOverwrite]:
    mapping: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    me = guild.me
    if me is not None:
        mapping[me] = _bot_overwrite()
    seen: set[int] = set()
    roles = getattr(snapshot, "rank_roles", {})
    for rank in STAFF_RANKS:
        for role_id in roles.get(rank, ()):
            if role_id in seen:
                continue
            seen.add(role_id)
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            mapping[role] = _member_overwrite()
    return mapping


def _archive_overwrites(guild: discord.Guild, snapshot: Any) -> dict[Any, discord.PermissionOverwrite]:
    mapping: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    me = guild.me
    if me is not None:
        mapping[me] = _bot_overwrite()
    seen: set[int] = set()
    roles = getattr(snapshot, "rank_roles", {})
    allow = _archive_reader_overwrite()
    for rank in (Rank.CHIEF, Rank.SPECIAL):
        for role_id in roles.get(rank, ()):
            if role_id in seen:
                continue
            seen.add(int(role_id))
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            mapping[role] = allow
    rule = snapshot.rule(GROUP_ARCHIVE_READ) if hasattr(snapshot, "rule") else None
    extra = getattr(rule, "extra_role_ids", frozenset()) if rule is not None else frozenset()
    for role_id in extra:
        if int(role_id) in seen:
            continue
        seen.add(int(role_id))
        role = guild.get_role(int(role_id))
        if role is None:
            continue
        mapping[role] = allow
    return mapping


def _overwrites(
    guild: discord.Guild,
    member_ids: tuple[int, ...],
    snapshot: Any,
) -> dict[Any, discord.PermissionOverwrite]:
    mapping = _staff_overwrites(guild, snapshot)
    allow = _member_overwrite()
    for user_id in member_ids:
        member = guild.get_member(int(user_id))
        mapping[member if member is not None else discord.Object(id=int(user_id))] = allow
    return mapping


def _hub_settings(hub: GroupHub) -> GroupHubSettings:
    return GroupHubSettings(
        category_id=int(hub.category_id),
        archive_category_id=None if hub.archive_category_id is None else int(hub.archive_category_id),
        silence_after=timedelta(seconds=max(60, int(hub.silence_seconds))),
        archive_days=max(0, int(hub.archive_days)),
        min_reputation=int(hub.min_reputation) if hub.min_reputation is not None else DEFAULT_MIN_REPUTATION,
        daily_limit=int(hub.daily_limit) if hub.daily_limit is not None else DEFAULT_DAILY_LIMIT,
        open_limit=int(hub.open_limit) if hub.open_limit is not None else DEFAULT_OPEN_LIMIT,
    )


async def _voice_has_messages(channel: Any) -> bool:
    if getattr(channel, "last_message_id", None):
        return True
    history = getattr(channel, "history", None)
    if not callable(history):
        return False
    try:
        async for _item in history(limit=1):
            return True
    except (discord.Forbidden, discord.HTTPException):
        return False
    return False
