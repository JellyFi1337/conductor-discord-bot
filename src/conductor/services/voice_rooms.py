"""Временные комнаты: хаб, одна комната на человека, уборка пустых, панель владельца."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Any

import discord
import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    VerificationSettings,
    VoiceHub,
    VoiceRoom,
    VoiceRoomKick,
    VoiceRoomLfg,
    VoiceRoomMerge,
    VoiceRoomRequest,
)
from conductor.db.session import session_scope
from conductor.domain.access import Rank, resolve_rank
from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_DELETE
from conductor.domain.observe import should_emit
from conductor.domain.renames import Deferred
from conductor.domain.rooms import (
    CATEGORY_CHANNEL_CAP,
    CATEGORY_HEAL_INTERVAL,
    CONFIG_DENIALS,
    CREATE_DEBOUNCE,
    DENIAL_NOTIFY_INTERVAL,
    EMPTY_DELETE_DELAY,
    GUILD_CHANNEL_CAP,
    NAME_TEMPLATE_DEFAULT,
    VOICE_LIMIT_MAX,
    ClaimVerdict,
    HubJoin,
    HubPreset,
    HubSettings,
    JoinAsk,
    MergeAsk,
    NameReject,
    RoomDenial,
    RoomOutcome,
    RoomReconcilePlan,
    StoredRoom,
    ask_merge,
    ask_to_join,
    check_room_name,
    check_room_status,
    claim_verdict,
    clone_uses_template,
    create_job_key,
    dump_hub_settings,
    empty_job_key,
    hub_join_action,
    kicked_may_enter,
    owner_overwrite,
    parse_bitrate,
    parse_hub_settings,
    parse_voice_limit,
    prepare_room_name,
    reconcile_rooms,
    room_for_owner,
    template_has_numbering,
)
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import edit_card, panel, send_card
from conductor.presentation.motion import MessageTarget, Progress, StageState
from conductor.presentation.tokens import Tone
from conductor.presentation.voice_panel import PANEL_KIND, RoomCard, owner_panel_view, voice_hub_denial_card
from conductor.services.access import cached_snapshot
from conductor.services.create_lock import continue_after_hold, hold_for, log_refused, note_success, trip
from conductor.services.panels import find_live_panel, mark_stale_by_ref, record_panel
from conductor.services.queue import ActionQueue
from conductor.services.runtime import log_tick
from conductor.services.structure import note_self_deleted
from conductor.services.traffic import lane

log = structlog.get_logger("conductor.rooms")
_INVITE_TTL = timedelta(minutes=15)

__all__ = ["EMPTY_DELETE_DELAY", "RoomJanitor", "VoiceRoomService", "humans_in", "to_stored"]

_CREATE_REASON = copy.REASON_VOICE
_BOT_NEED = ("manage_channels", "move_members", "connect")
_MEMBER_NEED = ("view_channel", "connect")


def to_stored(row: VoiceRoom) -> StoredRoom:
    return StoredRoom(
        guild_id=row.guild_id,
        channel_id=row.channel_id,
        owner_id=row.owner_id,
        hub_id=row.hub_id,
        id=row.id,
        locked=row.locked,
        owner_absent_since=row.owner_absent_since,
        ghosted=row.ghosted,
        text_channel_id=row.text_channel_id,
    )


def humans_in(channel: object | None) -> int:
    members = getattr(channel, "members", ()) or ()
    return sum(1 for member in members if not getattr(member, "bot", False))


def owner_in_channel(channel: object | None, owner_id: int) -> bool:
    members = getattr(channel, "members", ()) or ()
    return any(getattr(member, "id", None) == owner_id and not getattr(member, "bot", False) for member in members)


class RoomJanitor:
    """Одно отложенное удаление на комнату. Повторный пустой выход не ставит второе."""

    def __init__(
        self,
        queue: ActionQueue,
        sweep: Any,
        *,
        delay: float = EMPTY_DELETE_DELAY,
    ) -> None:
        self._queue = queue
        self._sweep = sweep
        self.delay = delay

    def mark_empty(self, guild_id: int, channel_id: int) -> None:
        key = empty_job_key(guild_id, channel_id)
        self._queue.schedule_later(key, self.delay, lambda: self._sweep(guild_id, channel_id))

    def mark_occupied(self, guild_id: int, channel_id: int) -> bool:
        return self._queue.cancel_later(empty_job_key(guild_id, channel_id))


class VoiceRoomService:
    def __init__(self, bot: Any, *, delay: float = EMPTY_DELETE_DELAY) -> None:
        self._bot = bot
        self.janitor = RoomJanitor(bot.queue, self.sweep_empty, delay=delay)
        self._category_healed: dict[int, datetime] = {}
        self._dm_denial_at: dict[tuple[int, int], datetime] = {}
        self._structure_denial_at: dict[tuple[int, str], datetime] = {}

    async def set_hub(
        self,
        session: AsyncSession,
        guild_id: int,
        *,
        channel_id: int,
        category_id: int | None,
    ) -> VoiceHub:
        row = await session.get(VoiceHub, guild_id)
        if row is None:
            row = VoiceHub(guild_id=guild_id, channel_id=channel_id, category_id=category_id, settings="{}")
            session.add(row)
        else:
            row.channel_id = channel_id
            row.category_id = category_id
        await session.flush()
        return row

    async def hub_for(self, session: AsyncSession, guild_id: int) -> VoiceHub | None:
        return await session.get(VoiceHub, guild_id)

    async def rooms_for(self, session: AsyncSession, guild_id: int) -> list[StoredRoom]:
        rows = (await session.scalars(select(VoiceRoom).where(VoiceRoom.guild_id == guild_id))).all()
        return [to_stored(row) for row in rows]

    async def is_room_channel(self, session: AsyncSession, guild_id: int, channel_id: int) -> bool:
        row = await session.scalar(
            select(VoiceRoom.id).where(VoiceRoom.guild_id == guild_id, VoiceRoom.channel_id == channel_id)
        )
        return row is not None

    async def room_by_channel(self, guild_id: int, channel_id: int) -> StoredRoom | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(VoiceRoom).where(VoiceRoom.guild_id == guild_id, VoiceRoom.channel_id == channel_id)
            )
            return None if row is None else to_stored(row)

    async def create_for(
        self,
        member: discord.Member,
        *,
        name: str | None = None,
        user_limit: int | None = None,
    ) -> RoomOutcome:
        """Комната на человека из хаба. Сбор 7d открывает её организатору."""
        guild = member.guild
        denial, _details = await self.hub_health(guild)
        if denial is not None:
            return RoomOutcome(denial=denial)
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild.id)
            rooms = await self.rooms_for(session, guild.id)
        if hub is None:
            return RoomOutcome(denial=RoomDenial.NO_HUB)
        existing = room_for_owner(rooms, member.id)
        if existing is not None:
            return RoomOutcome(channel_id=existing.channel_id)
        return await self._create_room(member, hub, name=name, user_limit=user_limit)

    async def hub_health(self, guild: discord.Guild) -> tuple[RoomDenial | None, dict[str, Any]]:
        """Предполёт: хаб, канал, категория, права бота и верифицированных."""
        guild_id = int(guild.id)
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild_id)
        if hub is None:
            return RoomDenial.NO_HUB, {"guild_id": guild_id}
        hub_id = int(hub.channel_id)
        stored_category = None if hub.category_id is None else int(hub.category_id)
        channel = guild.get_channel(hub_id)
        details: dict[str, Any] = {
            "guild_id": guild_id,
            "hub_id": hub_id,
            "category_id": stored_category,
        }
        if channel is None or not _is_voice(channel):
            return RoomDenial.HUB_GONE, details
        actual = _live_category_id(channel)
        category = None if actual is None else guild.get_channel(int(actual))
        if not _is_category(category):
            category = getattr(channel, "category", None)
            actual = None if category is None else getattr(category, "id", None)
            actual = None if actual is None else int(actual)
        if not _is_category(category) or actual is None:
            return RoomDenial.NO_CATEGORY, details
        details["category_id"] = int(actual)
        if stored_category != int(actual):
            await self._heal_category_id(guild, stored=stored_category, actual=int(actual))
        me = getattr(guild, "me", None)
        probe = getattr(category, "permissions_for", None)
        bot_perms = probe(me) if me is not None and callable(probe) else None
        if not _has_flags(bot_perms, _BOT_NEED):
            details["target"] = "bot"
            return RoomDenial.BOT_PERMS, details
        verified = await self._verified_role(guild)
        if verified is not None and not _role_can(channel, verified, _MEMBER_NEED, category=category):
            details["target"] = "verified"
            details["verified_role_id"] = int(getattr(verified, "id", 0) or 0)
            return RoomDenial.BOT_PERMS, details
        return None, details

    async def repair_hub(self, guild: discord.Guild, actor_id: int) -> tuple[RoomDenial | None, dict[str, Any]]:
        seed = getattr(self._bot, "seed", None)
        repair = getattr(seed, "repair", None)
        if callable(repair):
            from conductor.domain.seed import SeedMode

            await repair(guild, "hub", actor_id=int(actor_id), mode=SeedMode.BUILD)
        reapply = getattr(seed, "reapply_rights", None)
        if callable(reapply):
            await reapply(guild, actor_id=int(actor_id), keys=frozenset({"hub", "cat_rooms"}))
        return await self.hub_health(guild)

    async def guard_resource(self, guild_id: int, ref: int | str) -> GuardResource | None:
        try:
            room_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoom, room_id)
            if row is None:
                return None
            return GuardResource(
                ref=str(row.id),
                guild_id=row.guild_id,
                owner_id=row.owner_id,
                channel_id=row.channel_id,
            )

    async def handle_move(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot:
            return
        guild = member.guild
        before_id = None if before.channel is None else before.channel.id
        after_id = None if after.channel is None else after.channel.id
        if before_id == after_id:
            return
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild.id)
            rooms = await self.rooms_for(session, guild.id)
        skip_empty: set[int] = set()
        hub_id = None if hub is None else hub.channel_id
        in_hub = hub_id is not None and after_id == hub_id
        existing = room_for_owner(rooms, member.id)
        action = hub_join_action(in_hub=in_hub, is_bot=False, existing=existing)
        if action is HubJoin.MOVE and existing is not None:
            skip_empty.add(existing.channel_id)
            self.janitor.mark_occupied(guild.id, existing.channel_id)
            await self._move(member, existing.channel_id)
        elif action is HubJoin.CREATE:
            created = await self.create_for(member)
            if created.ok and created.channel_id is not None:
                skip_empty.add(created.channel_id)
            elif created.denial is not None:
                await self._note_denial(member, created)
        room_ids = {room.channel_id: room for room in rooms}
        if after_id in room_ids:
            target = room_ids[after_id]
            if not await self._kicked_may_enter(target.id, member.id):
                await self._disconnect(member)
            else:
                self.janitor.mark_occupied(guild.id, after_id)
        if before_id in room_ids and before_id not in skip_empty:
            channel = guild.get_channel(before_id)
            if humans_in(channel) == 0:
                self.janitor.mark_empty(guild.id, before_id)
        await self._sync_absence(guild.id, rooms, member.id, before_id=before_id, after_id=after_id)

    async def restore(self, guild: discord.Guild) -> RoomReconcilePlan | None:
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild.id)
            rooms = await self.rooms_for(session, guild.id)
        if not rooms and hub is None:
            return None
        hub_ids = () if hub is None else (hub.channel_id,)
        channels = [channel for channel in guild.voice_channels]
        existing = {channel.id for channel in channels}
        occupied = occupancy_map(channels)
        plan = reconcile_rooms(rooms, hub_ids=hub_ids, existing_channel_ids=existing, occupied=occupied)
        now = datetime.now(UTC)
        for channel_id in plan.keep:
            room = next((item for item in rooms if item.channel_id == channel_id), None)
            if room is None:
                continue
            self._bot.caches.room_owner.set((guild.id, channel_id), room.owner_id)
            self.janitor.mark_occupied(guild.id, channel_id)
            channel = guild.get_channel(channel_id)
            if owner_in_channel(channel, room.owner_id):
                await self._set_absent(room.id, None)
            elif room.owner_absent_since is None:
                await self._set_absent(room.id, now)
            if isinstance(channel, discord.VoiceChannel):
                await self._grant_owner_voice(channel, room.owner_id)
        for channel_id in plan.drop:
            await self._drop_row(guild.id, channel_id)
        for channel_id in plan.delete:
            await self.sweep_empty(guild.id, channel_id)
        await self.cleanup_companions(guild)
        return plan

    async def sweep_empty(self, guild_id: int, channel_id: int) -> bool:
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return False
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild_id)
            room = await session.scalar(
                select(VoiceRoom).where(VoiceRoom.guild_id == guild_id, VoiceRoom.channel_id == channel_id)
            )
        if hub is not None and channel_id == hub.channel_id:
            return False
        channel = guild.get_channel(channel_id)
        if room is None:
            return False
        if channel is None:
            await self._drop_row(guild_id, channel_id)
            return True
        if humans_in(channel) > 0:
            self.janitor.mark_occupied(guild_id, channel_id)
            return False
        hold = await hold_for(self._bot, guild_id, BUCKET_DELETE)
        if hold is not None:
            log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
            return False
        try:
            note_self_deleted(self._bot, guild_id, int(channel.id))
            await self._discord(channel.delete(reason=copy.REASON_VOICE_EMPTY))
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild_id, BUCKET_DELETE, exc)
            if hold is not None:
                log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
            log.warning("voice_delete_failed", guild_id=guild_id, channel_id=channel_id, error=str(exc))
            return False
        note_success(self._bot, guild_id, BUCKET_DELETE)
        await self._drop_row(guild_id, channel_id)
        await self._prune_extra_categories(guild)
        log_tick(log, "voice_empty_sweep", changed=1, guild_id=guild_id, channel_id=channel_id)
        return True

    async def toggle_lock(self, guild: discord.Guild | None, ref: int | str) -> bool | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return None
        locked = not room.locked
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return None
        try:
            await self._apply_everyone(channel, owner_id=room.owner_id, locked=locked, ghosted=room.ghosted)
        except discord.HTTPException as exc:
            log.warning("voice_lock_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return None
        async with session_scope(self._bot.sessions) as session:
            await session.execute(update(VoiceRoom).where(VoiceRoom.id == room.id).values(locked=locked))
        await self.refresh_panel(guild, room.id)
        return locked

    async def rename_room(self, guild: discord.Guild | None, ref: int | str, raw: str) -> str | bool | Deferred:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return NameReject.EMPTY.value
        prefix = await self._prefix(guild.id)
        checked = check_room_name(raw, prefix=prefix)
        if not checked.ok:
            return checked.reason
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return NameReject.EMPTY.value
        renames = getattr(self._bot, "renames", None)
        rename = getattr(renames, "rename", None)
        if not callable(rename):
            return NameReject.EMPTY.value
        result = await rename(channel, checked.name, key="voice", reason=_CREATE_REASON)
        if isinstance(result, Deferred):
            return result
        if str(getattr(channel, "name", "") or "") != checked.name:
            return NameReject.EMPTY.value
        await self.refresh_panel(guild, room.id)
        return True

    async def set_user_limit(self, guild: discord.Guild | None, ref: int | str, raw: str) -> bool:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return False
        limit = parse_voice_limit(raw)
        if limit is None:
            return False
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return False
        try:
            await self._discord(channel.edit(user_limit=limit, reason=_CREATE_REASON))
        except discord.HTTPException as exc:
            log.warning("voice_limit_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return False
        await self.refresh_panel(guild, room.id)
        return True

    async def transfer_room(self, guild: discord.Guild | None, ref: int | str, target: object) -> ClaimVerdict:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return ClaimVerdict.TOO_SOON
        target_id = int(target.id)
        if getattr(target, "bot", False):
            return ClaimVerdict.NOT_IN_ROOM
        async with session_scope(self._bot.sessions) as session:
            rooms = await self.rooms_for(session, guild.id)
        if target_id == room.owner_id:
            return ClaimVerdict.ALREADY_OWNER
        if room_for_owner(rooms, target_id) is not None:
            return ClaimVerdict.HAS_ROOM
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None or not any(getattr(member, "id", None) == target_id for member in channel.members):
            return ClaimVerdict.NOT_IN_ROOM
        previous = room.owner_id
        if not await self._set_owner(room.id, target_id, guild.id, room.channel_id):
            return ClaimVerdict.HAS_ROOM
        await self._reassign_owner_overwrites(guild, room, previous_id=previous, owner_id=target_id)
        await self.refresh_panel(guild, room.id)
        return ClaimVerdict.OK

    async def claim_room(
        self,
        guild: discord.Guild | None,
        ref: int | str,
        actor: object,
        *,
        now: datetime,
        occupant: bool,
        staff: bool = False,
    ) -> ClaimVerdict:
        del staff
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return ClaimVerdict.TOO_SOON
        actor_id = int(actor.id)
        async with session_scope(self._bot.sessions) as session:
            rooms = await self.rooms_for(session, guild.id)
        channel = None if guild is None else self._voice_channel(guild, room.channel_id)
        verdict = claim_verdict(
            actor_id=actor_id,
            owner_id=room.owner_id,
            owner_in_channel=owner_in_channel(channel, room.owner_id),
            actor_has_room=room_for_owner(rooms, actor_id) is not None,
            absent_since=room.owner_absent_since,
            now=now,
        )
        if verdict is not ClaimVerdict.OK:
            return verdict
        if not occupant:
            return ClaimVerdict.TOO_SOON
        previous = room.owner_id
        if not await self._set_owner(room.id, actor_id, guild.id, room.channel_id):
            return ClaimVerdict.HAS_ROOM
        await self._reassign_owner_overwrites(guild, room, previous_id=previous, owner_id=actor_id)
        await self.refresh_panel(guild, room.id)
        return ClaimVerdict.OK

    async def take_as_staff(self, guild: discord.Guild | None, ref: int | str, actor: object) -> ClaimVerdict:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return ClaimVerdict.TOO_SOON
        actor_id = int(actor.id)
        async with session_scope(self._bot.sessions) as session:
            rooms = await self.rooms_for(session, guild.id)
        if actor_id == room.owner_id:
            return ClaimVerdict.ALREADY_OWNER
        if room_for_owner(rooms, actor_id) is not None:
            return ClaimVerdict.HAS_ROOM
        previous = room.owner_id
        if not await self._set_owner(room.id, actor_id, guild.id, room.channel_id):
            return ClaimVerdict.HAS_ROOM
        await self._reassign_owner_overwrites(guild, room, previous_id=previous, owner_id=actor_id)
        self._log_take(guild.id, previous=previous, actor_id=actor_id, room=room)
        await self.refresh_panel(guild, room.id)
        return ClaimVerdict.OK

    async def kick_member(self, guild: discord.Guild | None, ref: int | str, target: object) -> str | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return copy.STALE_BODY
        target_id = int(target.id)
        if target_id == room.owner_id:
            return copy.VOICE_KICK_SELF
        blocked = await self._exclusion_denied(guild, target)
        if blocked is not None:
            return blocked
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None or not any(getattr(member, "id", None) == target_id for member in channel.members):
            return copy.VOICE_KICK_MISSING
        member = guild.get_member(target_id)
        if member is None:
            return copy.VOICE_KICK_MISSING
        await self._disconnect(member)
        await self.refresh_panel(guild, room.id)
        return None

    async def block_member(self, guild: discord.Guild | None, ref: int | str, target: object) -> str | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return copy.STALE_BODY
        target_id = int(target.id)
        if target_id == room.owner_id:
            return copy.VOICE_KICK_SELF
        blocked = await self._exclusion_denied(guild, target)
        if blocked is not None:
            return blocked
        member = guild.get_member(target_id)
        channel = self._voice_channel(guild, room.channel_id)
        if member is None or channel is None:
            return copy.STALE_BODY
        await self._record_kick(room.id, target_id)
        try:
            await self._set_member_access(channel, member, connect=False, view=False if room.ghosted else None)
        except discord.HTTPException as exc:
            log.warning("voice_ban_deny_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
        voice = member.voice
        here = voice is not None and voice.channel is not None and voice.channel.id == room.channel_id
        if here:
            await self._disconnect(member)
        await self.refresh_panel(guild, room.id)
        if _is_administrator(member):
            return copy.VOICE_BAN_ADMIN
        return None

    async def permit_member(self, guild: discord.Guild | None, ref: int | str, target: object) -> str | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return copy.STALE_BODY
        target_id = int(target.id)
        if getattr(target, "bot", False) or target_id == room.owner_id:
            return copy.VOICE_KICK_SELF if target_id == room.owner_id else copy.VOICE_TRANSFER_MISSING
        member = guild.get_member(target_id)
        channel = self._voice_channel(guild, room.channel_id)
        if member is None or channel is None:
            return copy.STALE_BODY
        try:
            await self._set_member_access(
                channel, member, connect=True, view=True, chat=True
            )
        except discord.HTTPException as exc:
            log.warning("voice_permit_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return copy.STALE_BODY
        await self._clear_kick(room.id, target_id)
        await self._delete_request(room.id, target_id)
        if member.voice is not None:
            await self._move(member, room.channel_id)
        await self.refresh_panel(guild, room.id)
        return None

    async def reject_member(self, guild: discord.Guild | None, ref: int | str, target: object) -> str | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return copy.STALE_BODY
        target_id = int(target.id)
        if target_id == room.owner_id:
            return copy.VOICE_KICK_SELF
        member = guild.get_member(target_id)
        channel = self._voice_channel(guild, room.channel_id)
        if member is None or channel is None:
            return copy.STALE_BODY
        try:
            await self._set_member_access(channel, member, connect=False, view=False if room.ghosted else None)
        except discord.HTTPException as exc:
            log.warning("voice_reject_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return copy.STALE_BODY
        await self._delete_request(room.id, target_id)
        voice = member.voice
        here = voice is not None and voice.channel is not None and voice.channel.id == room.channel_id
        if here:
            await self._disconnect(member)
        await self.refresh_panel(guild, room.id)
        return None

    async def toggle_ghost(self, guild: discord.Guild | None, ref: int | str) -> bool | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return None
        ghosted = not room.ghosted
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return None
        try:
            await self._apply_everyone(channel, owner_id=room.owner_id, locked=room.locked, ghosted=ghosted)
            if ghosted:
                for occupant in channel.members:
                    if occupant.bot or occupant.id == room.owner_id:
                        continue
                    await self._set_member_access(channel, occupant, connect=None, view=True)
        except discord.HTTPException as exc:
            log.warning("voice_ghost_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return None
        async with session_scope(self._bot.sessions) as session:
            await session.execute(update(VoiceRoom).where(VoiceRoom.id == room.id).values(ghosted=ghosted))
        await self.refresh_panel(guild, room.id)
        return ghosted

    async def ask_join(self, guild: discord.Guild | None, ref: int | str, actor: object) -> JoinAsk:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return JoinAsk.OPEN
        actor_id = int(actor.id)
        channel = self._voice_channel(guild, room.channel_id)
        async with session_scope(self._bot.sessions) as session:
            pending = await session.scalar(
                select(VoiceRoomRequest.id).where(
                    VoiceRoomRequest.room_id == room.id, VoiceRoomRequest.user_id == actor_id
                )
            )
        verdict = ask_to_join(
            actor_id=actor_id,
            owner_id=room.owner_id,
            locked=room.locked,
            pending=pending is not None,
            kicked=not await self._kicked_may_enter(room.id, actor_id),
            inside=any(getattr(member, "id", None) == actor_id for member in getattr(channel, "members", ())),
        )
        if verdict is not JoinAsk.OK:
            return verdict
        try:
            async with session_scope(self._bot.sessions) as session:
                session.add(VoiceRoomRequest(room_id=room.id, user_id=actor_id, created_at=datetime.now(UTC)))
                await session.flush()
        except IntegrityError:
            return JoinAsk.PENDING
        await self.refresh_panel(guild, room.id)
        return JoinAsk.OK

    async def list_requests(self, guild: discord.Guild | None, ref: int | str) -> list[tuple[int, int]]:
        room = await self._live_room(guild, ref)
        if room is None:
            return []
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(VoiceRoomRequest)
                    .where(VoiceRoomRequest.room_id == room.id)
                    .order_by(VoiceRoomRequest.created_at)
                )
            ).all()
        return [(row.id, row.user_id) for row in rows]

    async def resolve_request(self, guild: discord.Guild | None, ref: int | str, *, allow: bool) -> str | None:
        try:
            request_id = int(ref)
        except (TypeError, ValueError):
            return copy.STALE_BODY
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomRequest, request_id)
            if row is None:
                return copy.STALE_BODY
            user_id = row.user_id
            room_id = row.room_id
        if guild is None:
            return copy.STALE_BODY
        target = guild.get_member(user_id) or discord.Object(id=user_id)
        if allow:
            return await self.permit_member(guild, room_id, target)
        error = await self.reject_member(guild, room_id, target)
        await self._delete_request(room_id, user_id)
        return error

    async def one_time_invite(self, guild: discord.Guild | None, ref: int | str) -> str | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return None
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return None
        try:
            invite = await self._discord(
                channel.create_invite(
                    max_uses=1,
                    max_age=int(_INVITE_TTL.total_seconds()),
                    unique=True,
                    reason=_CREATE_REASON,
                )
            )
        except discord.HTTPException as exc:
            log.warning("voice_invite_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return None
        return str(invite.url)

    async def configure_preset(
        self,
        guild: discord.Guild | None,
        *,
        preset: str,
        template: str | None = None,
        template_channel_id: int | None = None,
    ) -> HubSettings | None:
        if guild is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild.id)
            if hub is None:
                return None
            current = parse_hub_settings(hub.settings)
            if preset == "sequential":
                preset = HubPreset.NAME
            if preset == HubPreset.CLONE:
                if template_channel_id is None or template_channel_id == hub.channel_id:
                    return None
                if await self.is_room_channel(session, guild.id, template_channel_id):
                    return None
            chosen = template if template is not None else current.template or NAME_TEMPLATE_DEFAULT
            if template_has_numbering(chosen):
                return None
            updated = HubSettings(
                prefix=current.prefix,
                template=chosen,
                preset=preset,
                template_channel_id=template_channel_id if preset == HubPreset.CLONE else None,
                extra_category_ids=current.extra_category_ids,
            )
            hub.settings = dump_hub_settings(updated)
        return updated

    async def set_bitrate(self, guild: discord.Guild | None, ref: int | str, raw: str) -> bool:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return False
        kbps = parse_bitrate(raw)
        if kbps is None:
            return False
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return False
        try:
            await self._discord(channel.edit(bitrate=kbps * 1000, reason=_CREATE_REASON))
        except discord.HTTPException as exc:
            log.warning("voice_bitrate_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return False
        return True

    async def set_status(self, guild: discord.Guild | None, ref: int | str, raw: str) -> bool | None:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return None
        checked = check_room_status(raw)
        if not checked.ok:
            return False
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return None
        try:
            await self._discord(channel.edit(status=checked.name or None, reason=_CREATE_REASON))
        except TypeError:
            return False
        except discord.HTTPException as exc:
            log.warning("voice_status_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return False
        return True

    async def request_merge(self, guild: discord.Guild | None, ref: int | str, target: object) -> MergeAsk:
        room = await self._live_room(guild, ref)
        if room is None or guild is None:
            return MergeAsk.MISSING
        target_id = int(getattr(target, "id", 0) or 0)
        dest = await self.room_by_channel(guild.id, target_id)
        if dest is None:
            return MergeAsk.MISSING
        async with session_scope(self._bot.sessions) as session:
            pending = await session.scalar(select(VoiceRoomMerge.id).where(VoiceRoomMerge.from_room_id == room.id))
        verdict = ask_merge(from_id=room.id, to_id=dest.id, pending_from=pending)
        if verdict is not MergeAsk.OK:
            return verdict
        try:
            async with session_scope(self._bot.sessions) as session:
                session.add(VoiceRoomMerge(from_room_id=room.id, to_room_id=dest.id, created_at=datetime.now(UTC)))
                await session.flush()
        except IntegrityError:
            return MergeAsk.PENDING
        await self.refresh_panel(guild, dest.id)
        return MergeAsk.OK

    async def accept_merge(self, guild: discord.Guild | None, ref: int | str) -> str | None:
        try:
            merge_id = int(ref)
        except (TypeError, ValueError):
            return copy.STALE_BODY
        if guild is None:
            return copy.STALE_BODY
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomMerge, merge_id)
            if row is None:
                return copy.STALE_BODY
            source = await session.get(VoiceRoom, row.from_room_id)
            dest = await session.get(VoiceRoom, row.to_room_id)
            if source is None or dest is None or dest.guild_id != guild.id:
                return copy.STALE_BODY
            from_id = source.channel_id
            to_id = dest.channel_id
            dest_room_id = dest.id
        origin = self._voice_channel(guild, from_id)
        if origin is None:
            return copy.STALE_BODY
        for occupant in list(origin.members):
            if occupant.bot:
                continue
            await self._move(occupant, to_id)
        await self.sweep_empty(guild.id, from_id)
        await self.refresh_panel(guild, dest_room_id)
        return None

    async def guard_merge(self, guild_id: int, ref: int | str) -> GuardResource | None:
        try:
            merge_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomMerge, merge_id)
            if row is None:
                return None
            room = await session.get(VoiceRoom, row.to_room_id)
            if room is None or room.guild_id != guild_id:
                return None
            return GuardResource(
                ref=str(row.id),
                guild_id=room.guild_id,
                owner_id=room.owner_id,
                channel_id=room.channel_id,
            )

    async def guard_request(self, guild_id: int, ref: int | str) -> GuardResource | None:
        try:
            request_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomRequest, request_id)
            if row is None:
                return None
            room = await session.get(VoiceRoom, row.room_id)
            if room is None:
                return None
            return GuardResource(
                ref=str(row.id),
                guild_id=room.guild_id,
                owner_id=room.owner_id,
                channel_id=room.channel_id,
            )

    async def restore_panel(self, guild: discord.Guild, room: StoredRoom, *, actor_id: int) -> bool:
        channel = self._voice_channel(guild, room.channel_id)
        if channel is None:
            return False
        view = await self._panel_card(guild, room, channel)
        async with session_scope(self._bot.sessions) as session:
            existing = await find_live_panel(session, guild_id=guild.id, kind=PANEL_KIND, ref=room.id)
            snapshot = None if existing is None else (existing.channel_id, existing.message_id)
        if snapshot is not None:
            posted = guild.get_channel(snapshot[0])
            fetch = getattr(posted, "fetch_message", None)
            if callable(fetch):
                try:
                    message = await fetch(snapshot[1])
                    await edit_card(message, view)
                    return True
                except discord.HTTPException:
                    pass
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("voice_panel_send_failed", guild_id=guild.id, room_id=room.id, error=str(exc))
            return False
        async with session_scope(self._bot.sessions) as session:
            await mark_stale_by_ref(session, guild_id=guild.id, kind=PANEL_KIND, ref=room.id)
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=channel.id,
                message_id=message.id,
                ref=room.id,
                created_by=actor_id,
            )
        return True

    async def refresh_panel(self, guild: discord.Guild, room_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoom, room_id)
            room = None if row is None else to_stored(row)
            existing = await find_live_panel(session, guild_id=guild.id, kind=PANEL_KIND, ref=room_id)
            snapshot = None if existing is None else (existing.channel_id, existing.message_id)
        if room is None or snapshot is None:
            return
        channel = self._voice_channel(guild, room.channel_id)
        posted = guild.get_channel(snapshot[0])
        fetch = getattr(posted, "fetch_message", None)
        if channel is None or not callable(fetch):
            return
        try:
            message = await fetch(snapshot[1])
            view = await self._panel_card(guild, room, channel)
            await edit_card(message, view)
        except discord.HTTPException as exc:
            log.warning("voice_panel_refresh_failed", guild_id=guild.id, room_id=room_id, error=str(exc))

    async def _create_room(
        self,
        member: discord.Member,
        hub: VoiceHub,
        *,
        name: str | None = None,
        user_limit: int | None = None,
    ) -> RoomOutcome:
        guild = member.guild
        if not self._bot.queue.debounce(create_job_key(guild.id, member.id), CREATE_DEBOUNCE):
            return RoomOutcome(denial=RoomDenial.RATE)
        if _guild_is_full(guild):
            return RoomOutcome(denial=RoomDenial.LIMIT)
        settings = parse_hub_settings(hub.settings)
        category = _first_open_category(guild, hub, settings)
        need_overflow = category is None and _primary_category(guild, hub) is not None
        if need_overflow and _guild_is_full(guild):
            return RoomOutcome(denial=RoomDenial.LIMIT)
        room_name = None
        if name:
            checked = check_room_name(name, prefix=settings.prefix)
            room_name = checked.name if checked.ok else None
        if not room_name:
            room_name, _updated = prepare_room_name(
                settings, username=member.display_name, game=_playing_name(member)
            )
        kwargs: dict[str, Any] = {"reason": copy.actor_reason(member.id, _CREATE_REASON)}
        if user_limit is not None:
            kwargs["user_limit"] = max(0, min(VOICE_LIMIT_MAX, int(user_limit)))
        if clone_uses_template(
            preset=settings.preset,
            template_channel_id=settings.template_channel_id,
            hub_id=hub.channel_id,
        ):
            source = guild.get_channel(int(settings.template_channel_id or 0))
            if isinstance(source, discord.VoiceChannel):
                kwargs["bitrate"] = source.bitrate
                kwargs["overwrites"] = dict(source.overwrites)
        overwrites = dict(kwargs.get("overwrites") or {})
        overwrites[member] = _permission_overwrite(owner_overwrite())
        kwargs["overwrites"] = overwrites
        with lane(Lane.USER):
            hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
        if hold is not None:
            log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            continue_after_hold(
                self._bot,
                ("voice-create-defer", guild.id, member.id),
                hold,
                lambda: self.create_for(member, name=name, user_limit=user_limit),
            )
            return RoomOutcome(denial=RoomDenial.HELD, hold=hold)
        if need_overflow:
            category = await self._open_overflow_category(guild, hub, settings)
            if category is None:
                return RoomOutcome(denial=RoomDenial.LIMIT)
        if category is not None:
            kwargs["category"] = category
        try:
            with lane(Lane.USER):
                channel = await self._discord(guild.create_voice_channel(room_name, **kwargs))
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            log.warning("voice_create_failed", guild_id=guild.id, user_id=member.id, error=str(exc))
            if getattr(exc, "code", None) == 30013:
                return RoomOutcome(denial=RoomDenial.LIMIT)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
                return RoomOutcome(denial=RoomDenial.HELD, hold=hold)
            status = getattr(exc, "status", None)
            if status == 429 or isinstance(exc, discord.RateLimited):
                return RoomOutcome(denial=RoomDenial.HELD)
            return RoomOutcome(denial=RoomDenial.FAILED)
        note_success(self._bot, guild.id, BUCKET_CHANNEL)
        stored = await self._insert_room(guild.id, channel_id=channel.id, owner_id=member.id, hub_id=hub.channel_id)
        if stored is None:
            await self._delete_orphan(channel)
            async with session_scope(self._bot.sessions) as session:
                rooms = await self.rooms_for(session, guild.id)
            existing = room_for_owner(rooms, member.id)
            if existing is not None:
                await self._move(member, existing.channel_id)
                return RoomOutcome(channel_id=existing.channel_id)
            return RoomOutcome(denial=RoomDenial.FAILED)
        self._bot.caches.room_owner.set((guild.id, channel.id), member.id)
        await self._announce_created(member, channel, stored)
        return RoomOutcome(channel_id=channel.id)

    async def _announce_created(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel,
        room: StoredRoom,
    ) -> None:
        stages = (
            copy.VOICE_STAGE_RIGHTS,
            copy.VOICE_STAGE_CHANNEL,
            copy.VOICE_STAGE_SAVE,
            copy.VOICE_STAGE_MOVE,
            copy.VOICE_STAGE_PANEL,
        )
        registry = getattr(self._bot, "motion", None)
        try:
            placeholder = panel(copy.VOICE_CREATE_TITLE, tone=Tone.RUNNING, timeout=None)
            mentions = discord.AllowedMentions.none()
            message = await self._discord(send_card(channel, placeholder, mentions=mentions))
        except discord.HTTPException as exc:
            log.warning("voice_create_card_failed", guild_id=member.guild.id, room_id=room.id, error=str(exc))
            await self._move(member, channel.id)
            return
        progress = Progress(
            MessageTarget(message),
            title=copy.VOICE_CREATE_TITLE,
            show_after=0,
            min_interval=0,
            registry=registry if hasattr(registry, "render") else None,
        )
        async with progress:
            await progress.stage(stages[0], StageState.DONE)
            await progress.stage(stages[1], StageState.DONE)
            await progress.stage(stages[2], StageState.DONE)
            await progress.stage(stages[3], StageState.RUNNING)
            await self._move(member, channel.id)
            await progress.stage(stages[3], StageState.DONE)
            await progress.stage(stages[4], StageState.RUNNING)
            view = await self._panel_card(member.guild, room, channel)
            await progress.finish(view)
        async with session_scope(self._bot.sessions) as session:
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=member.guild.id,
                channel_id=channel.id,
                message_id=message.id,
                ref=room.id,
                created_by=member.id,
            )

    async def _insert_room(
        self,
        guild_id: int,
        *,
        channel_id: int,
        owner_id: int,
        hub_id: int,
    ) -> StoredRoom | None:
        try:
            async with session_scope(self._bot.sessions) as session:
                row = VoiceRoom(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    owner_id=owner_id,
                    hub_id=hub_id,
                    created_at=datetime.now(UTC),
                    locked=False,
                )
                session.add(row)
                await session.flush()
                return to_stored(row)
        except IntegrityError:
            return None

    async def _drop_row(self, guild_id: int, channel_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(VoiceRoom).where(VoiceRoom.guild_id == guild_id, VoiceRoom.channel_id == channel_id)
            )
            if row is not None:
                text_id = row.text_channel_id
                await session.execute(delete(VoiceRoomKick).where(VoiceRoomKick.room_id == row.id))
                await session.execute(delete(VoiceRoomRequest).where(VoiceRoomRequest.room_id == row.id))
                await session.execute(delete(VoiceRoomLfg).where(VoiceRoomLfg.room_id == row.id))
                await session.execute(
                    delete(VoiceRoomMerge).where(
                        (VoiceRoomMerge.from_room_id == row.id) | (VoiceRoomMerge.to_room_id == row.id)
                    )
                )
                await mark_stale_by_ref(session, guild_id=guild_id, kind=PANEL_KIND, ref=row.id)
                await session.delete(row)
            else:
                text_id = None
        guild = self._bot.get_guild(guild_id)
        if guild is not None and text_id is not None:
            posted = guild.get_channel(text_id)
            if posted is not None:
                try:
                    note_self_deleted(self._bot, guild_id, int(posted.id))
                    await self._discord(posted.delete(reason=copy.REASON_VOICE_TEXT))
                except discord.HTTPException:
                    log.warning("voice_text_delete_failed", guild_id=guild_id, channel_id=text_id)
        caches = getattr(self._bot, "caches", None)
        invalidate = getattr(caches, "invalidate_room", None)
        if callable(invalidate):
            invalidate(guild_id, channel_id)

    async def _move(self, member: discord.Member, channel_id: int) -> None:
        guild = member.guild
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return
        if member.voice is not None and member.voice.channel is not None and member.voice.channel.id == channel_id:
            return
        try:
            await self._discord(member.move_to(channel, reason=_CREATE_REASON))
        except discord.HTTPException as exc:
            log.warning("voice_move_failed", guild_id=guild.id, user_id=member.id, error=str(exc))

    async def _delete_orphan(self, channel: discord.VoiceChannel) -> None:
        guild = getattr(channel, "guild", None)
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id:
            hold = await hold_for(self._bot, guild_id, BUCKET_DELETE)
            if hold is not None:
                log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
                return
        try:
            note_self_deleted(self._bot, guild_id, int(channel.id))
            await self._discord(channel.delete(reason=copy.REASON_VOICE_EXTRA))
        except (discord.HTTPException, discord.RateLimited) as exc:
            if guild_id:
                hold = trip(self._bot, guild_id, BUCKET_DELETE, exc)
                if hold is not None:
                    log_refused(guild_id, BUCKET_DELETE, hold, action="delete")
            log.warning("voice_orphan_delete_failed", channel_id=channel.id, error=str(exc))
            return
        if guild_id:
            note_success(self._bot, guild_id, BUCKET_DELETE)

    async def _live_room(self, guild: discord.Guild | None, ref: int | str) -> StoredRoom | None:
        if guild is None:
            return None
        try:
            room_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoom, room_id)
            if row is None or row.guild_id != guild.id:
                return None
            return to_stored(row)

    async def _prefix(self, guild_id: int) -> str:
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild_id)
        if hub is None:
            return ""
        return parse_hub_settings(hub.settings).prefix

    async def _set_owner(self, room_id: int, owner_id: int, guild_id: int, channel_id: int) -> bool:
        try:
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(VoiceRoom, room_id)
                if row is None:
                    return False
                row.owner_id = owner_id
                row.owner_absent_since = None
                await session.flush()
        except IntegrityError:
            return False
        caches = getattr(self._bot, "caches", None)
        setter = getattr(getattr(caches, "room_owner", None), "set", None)
        if callable(setter):
            setter((guild_id, channel_id), owner_id)
        return True

    async def _set_absent(self, room_id: int, when: datetime | None) -> None:
        async with session_scope(self._bot.sessions) as session:
            await session.execute(update(VoiceRoom).where(VoiceRoom.id == room_id).values(owner_absent_since=when))

    async def _sync_absence(
        self,
        guild_id: int,
        rooms: list[StoredRoom],
        user_id: int,
        *,
        before_id: int | None,
        after_id: int | None,
        now: datetime | None = None,
    ) -> None:
        stamp = now or datetime.now(UTC)
        for room in rooms:
            if room.owner_id != user_id:
                continue
            if before_id == room.channel_id and after_id != room.channel_id:
                await self._set_absent(room.id, stamp)
            if after_id == room.channel_id:
                await self._set_absent(room.id, None)

    async def _apply_everyone(
        self,
        channel: discord.VoiceChannel,
        *,
        owner_id: int,
        locked: bool,
        ghosted: bool,
    ) -> None:
        guild = channel.guild
        everyone = channel.overwrites_for(guild.default_role)
        everyone.connect = False if locked else None
        everyone.view_channel = False if ghosted else None
        await self._discord(channel.set_permissions(guild.default_role, overwrite=everyone, reason=_CREATE_REASON))
        await self._grant_owner_voice(channel, owner_id)

    async def _grant_owner_voice(self, channel: discord.VoiceChannel, owner_id: int) -> None:
        owner = channel.guild.get_member(owner_id)
        if owner is None:
            return
        try:
            await self._discord(
                channel.set_permissions(
                    owner, overwrite=_permission_overwrite(owner_overwrite()), reason=_CREATE_REASON
                )
            )
        except discord.HTTPException as exc:
            log.warning("voice_owner_overwrite_failed", guild_id=channel.guild.id, user_id=owner_id, error=str(exc))

    async def _reassign_owner_overwrites(
        self,
        guild: discord.Guild,
        room: StoredRoom,
        *,
        previous_id: int,
        owner_id: int,
    ) -> None:
        if previous_id == owner_id:
            return
        voice = self._voice_channel(guild, room.channel_id)
        if voice is not None:
            await self._edit_owner_overwrite(voice, previous_id=previous_id, owner_id=owner_id, flags=owner_overwrite())

    async def _edit_owner_overwrite(
        self,
        channel: discord.abc.GuildChannel,
        *,
        previous_id: int,
        owner_id: int,
        flags: dict[str, bool],
    ) -> None:
        overwrites = _without_user(dict(channel.overwrites), previous_id)
        owner = channel.guild.get_member(owner_id)
        if owner is not None:
            overwrites[owner] = _permission_overwrite(flags)
        try:
            await self._discord(channel.edit(overwrites=overwrites, reason=_CREATE_REASON))
        except discord.HTTPException as exc:
            log.warning(
                "voice_owner_overwrite_failed",
                guild_id=channel.guild.id,
                channel_id=channel.id,
                user_id=owner_id,
                error=str(exc),
            )

    async def _set_member_access(
        self,
        channel: discord.VoiceChannel,
        member: discord.abc.Snowflake,
        *,
        connect: bool | None,
        view: bool | None,
        chat: bool | None = None,
    ) -> None:
        overwrite = channel.overwrites_for(member)
        overwrite.connect = connect
        overwrite.view_channel = view
        if chat is True:
            overwrite.send_messages = True
            overwrite.read_message_history = True
        elif chat is False:
            overwrite.send_messages = False
            overwrite.read_message_history = False
        await self._discord(channel.set_permissions(member, overwrite=overwrite, reason=_CREATE_REASON))

    def _voice_channel(self, guild: discord.Guild, channel_id: int) -> discord.VoiceChannel | None:
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    async def _panel_card(
        self, guild: discord.Guild, room: StoredRoom, channel: discord.VoiceChannel
    ) -> discord.ui.LayoutView:
        merge_id = 0
        async with session_scope(self._bot.sessions) as session:
            incoming = await session.scalar(
                select(VoiceRoomMerge.id)
                .where(VoiceRoomMerge.to_room_id == room.id)
                .order_by(VoiceRoomMerge.created_at)
            )
        if incoming is not None:
            merge_id = int(incoming)
        lfg_line = None
        hook = getattr(getattr(self._bot, "lfg", None), "room_line", None)
        if callable(hook):
            lfg_line = await hook(guild.id, room.id)
        return owner_panel_view(
            RoomCard(
                room_id=room.id,
                name=channel.name,
                owner_id=room.owner_id,
                humans=humans_in(channel),
                limit=int(getattr(channel, "user_limit", 0) or 0),
                locked=room.locked,
                ghosted=room.ghosted,
                requests=await self._request_count(room.id),
                lfg_line=lfg_line,
                merge_id=merge_id,
            )
        )

    async def _request_count(self, room_id: int) -> int:
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(VoiceRoomRequest.id).where(VoiceRoomRequest.room_id == room_id))).all()
        return len(rows)

    async def _kicked_may_enter(self, room_id: int, user_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomKick, (room_id, user_id))
        return kicked_may_enter(kicked=row is not None)

    async def _record_kick(self, room_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            existing = await session.get(VoiceRoomKick, (room_id, user_id))
            if existing is None:
                session.add(VoiceRoomKick(room_id=room_id, user_id=user_id))

    async def _clear_kick(self, room_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VoiceRoomKick, (room_id, user_id))
            if row is not None:
                await session.delete(row)

    async def _delete_request(self, room_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(VoiceRoomRequest).where(VoiceRoomRequest.room_id == room_id, VoiceRoomRequest.user_id == user_id)
            )
            if row is not None:
                await session.delete(row)

    async def _save_hub_settings(self, guild_id: int, settings: HubSettings) -> None:
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild_id)
            if hub is not None:
                hub.settings = dump_hub_settings(settings)

    async def _clear_text_channel(self, room_id: int) -> None:
        """Спутников больше нет: колонку можно только обнулить."""
        async with session_scope(self._bot.sessions) as session:
            await session.execute(update(VoiceRoom).where(VoiceRoom.id == room_id).values(text_channel_id=None))

    async def cleanup_companions(self, guild: discord.Guild) -> int:
        """Удаляет старые текстовые спутники и обнуляет text_channel_id."""
        async with session_scope(self._bot.sessions) as session:
            rooms = await self.rooms_for(session, guild.id)
        cleared = 0
        for room in rooms:
            if room.text_channel_id is None:
                continue
            posted = guild.get_channel(room.text_channel_id)
            if posted is not None:
                with lane(Lane.BACKGROUND):
                    hold = await hold_for(self._bot, guild.id, BUCKET_DELETE)
                if hold is not None:
                    log_refused(guild.id, BUCKET_DELETE, hold, action="delete")
                    continue
                try:
                    note_self_deleted(self._bot, int(guild.id), int(posted.id))
                    with lane(Lane.BACKGROUND):
                        await self._discord(posted.delete(reason=copy.REASON_VOICE_TEXT))
                except (discord.HTTPException, discord.RateLimited) as exc:
                    hold = trip(self._bot, guild.id, BUCKET_DELETE, exc)
                    if hold is not None:
                        log_refused(guild.id, BUCKET_DELETE, hold, action="delete")
                    log.warning(
                        "voice_companion_delete_failed",
                        guild_id=guild.id,
                        channel_id=room.text_channel_id,
                        error=str(exc),
                    )
                    continue
                note_success(self._bot, guild.id, BUCKET_DELETE)
            await self._clear_text_channel(room.id)
            cleared += 1
        log.info("voice_companions_cleaned", guild_id=guild.id, count=cleared)
        return cleared

    async def _open_overflow_category(
        self, guild: discord.Guild, hub: VoiceHub, settings: HubSettings
    ) -> object | None:
        primary = _primary_category(guild, hub)
        if primary is None:
            return None
        if _guild_is_full(guild):
            return None
        overwrites = dict(getattr(primary, "overwrites", {}) or {})
        position = int(getattr(primary, "position", 0) or 0) + 1
        for cat_id in settings.extra_category_ids:
            extra = guild.get_channel(cat_id)
            pos = getattr(extra, "position", None) if extra is not None else None
            if isinstance(pos, int) and pos + 1 > position:
                position = pos + 1
        try:
            with lane(Lane.USER):
                created = await self._discord(
                    guild.create_category(
                        str(getattr(primary, "name", "") or copy.VOICE_TITLE),
                        overwrites=overwrites,
                        position=position,
                        reason=copy.REASON_VOICE_OVERFLOW,
                    )
                )
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            log.warning("voice_overflow_failed", guild_id=guild.id, error=str(exc))
            if getattr(exc, "code", None) == 30013:
                return None
            return None
        extra_id = int(created.id)
        updated = HubSettings(
            prefix=settings.prefix,
            template=settings.template,
            preset=settings.preset,
            template_channel_id=settings.template_channel_id,
            extra_category_ids=(*settings.extra_category_ids, extra_id),
        )
        await self._save_hub_settings(guild.id, updated)
        return created

    async def _prune_extra_categories(self, guild: discord.Guild) -> None:
        async with session_scope(self._bot.sessions) as session:
            hub = await self.hub_for(session, guild.id)
        if hub is None:
            return
        settings = parse_hub_settings(hub.settings)
        if not settings.extra_category_ids:
            return
        kept: list[int] = []
        for cat_id in settings.extra_category_ids:
            category = guild.get_channel(cat_id)
            if category is None or not _is_category(category):
                continue
            children = list(getattr(category, "channels", ()) or ())
            if children:
                kept.append(cat_id)
                continue
            hold = await hold_for(self._bot, guild.id, BUCKET_DELETE)
            if hold is not None:
                log_refused(guild.id, BUCKET_DELETE, hold, action="delete")
                kept.append(cat_id)
                continue
            try:
                note_self_deleted(self._bot, int(guild.id), int(cat_id))
                await self._discord(category.delete(reason=copy.REASON_VOICE_OVERFLOW))
            except (discord.HTTPException, discord.RateLimited) as exc:
                hold = trip(self._bot, guild.id, BUCKET_DELETE, exc)
                if hold is not None:
                    log_refused(guild.id, BUCKET_DELETE, hold, action="delete")
                log.warning("voice_overflow_delete_failed", guild_id=guild.id, category_id=cat_id, error=str(exc))
                kept.append(cat_id)
                continue
            note_success(self._bot, guild.id, BUCKET_DELETE)
        if tuple(kept) != settings.extra_category_ids:
            await self._save_hub_settings(
                guild.id,
                HubSettings(
                    prefix=settings.prefix,
                    template=settings.template,
                    preset=settings.preset,
                    template_channel_id=settings.template_channel_id,
                    extra_category_ids=tuple(kept),
                ),
            )

    async def _exclusion_denied(self, guild: discord.Guild, target: object) -> str | None:
        if getattr(target, "bot", False):
            return copy.VOICE_KICK_BOT
        store = getattr(getattr(self._bot, "caches", None), "permissions", None)
        if store is None:
            return None
        try:
            snapshot = await cached_snapshot(self._bot, int(guild.id))
        except (AttributeError, TypeError) as exc:
            log.warning("voice_kick_rank_failed", guild_id=guild.id, error=str(exc))
            return None
        role_ids = tuple(
            int(role.id)
            for role in getattr(target, "roles", ()) or ()
            if getattr(role, "id", None) is not None
        )
        if resolve_rank(role_ids, snapshot.rank_roles) >= Rank.WATCHER:
            return copy.VOICE_KICK_STAFF
        return None

    async def _disconnect(self, member: discord.Member) -> None:
        try:
            await self._discord(member.move_to(None, reason=_CREATE_REASON))
        except discord.HTTPException as exc:
            log.warning("voice_disconnect_failed", guild_id=member.guild.id, user_id=member.id, error=str(exc))

    async def _heal_category_id(self, guild: discord.Guild, *, stored: int | None, actual: int) -> None:
        guild_id = int(guild.id)
        if stored == actual:
            return
        now = datetime.now(UTC)
        if not should_emit(self._category_healed.get(guild_id), now=now, interval=CATEGORY_HEAL_INTERVAL):
            return
        changed = False
        async with session_scope(self._bot.sessions) as session:
            row = await self.hub_for(session, guild_id)
            current = None if row is None or row.category_id is None else int(row.category_id)
            if row is not None and current != actual:
                row.category_id = actual
                changed = True
        if not changed:
            return
        self._category_healed[guild_id] = now
        log.info("voice_hub_category_healed", guild_id=guild_id, before=stored, after=actual)
        self._post_structure(
            guild,
            panel(copy.DIAGNOSE_FEATURE_VOICE, copy.VOICE_HUB_CATEGORY_HEALED, tone=Tone.INFO, timeout=None),
        )

    async def _verified_role(self, guild: discord.Guild) -> object | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VerificationSettings, int(guild.id))
            role_id = None if row is None else row.verified_role_id
        if not role_id:
            return None
        getter = getattr(guild, "get_role", None)
        if not callable(getter):
            return None
        return getter(int(role_id))

    async def _note_denial(self, member: discord.Member, outcome: RoomOutcome) -> None:
        denial = outcome.denial
        if denial is None:
            return
        now = datetime.now(UTC)
        guild = member.guild
        dm_key = (int(guild.id), int(member.id))
        if should_emit(self._dm_denial_at.get(dm_key), now=now, interval=DENIAL_NOTIFY_INTERVAL):
            body = copy.voice_denial_text(denial.value)
            if outcome.hold is not None:
                hold = outcome.hold
                body = copy.voice_hold_text(str(hold.source), hold.retry_after, route=hold.route)
            view = panel(copy.VOICE_CREATE_TITLE, body, tone=Tone.WARN)
            if await self._send_dm(member, view):
                self._dm_denial_at[dm_key] = now
        if denial not in CONFIG_DENIALS:
            return
        structure_key = (int(guild.id), denial.value)
        if not should_emit(self._structure_denial_at.get(structure_key), now=now, interval=DENIAL_NOTIFY_INTERVAL):
            return
        self._post_structure(guild, voice_hub_denial_card(denial.value))
        self._structure_denial_at[structure_key] = now

    async def _send_dm(self, target: object, view: object) -> bool:
        send = getattr(target, "send", None)
        if not callable(send):
            return False
        try:
            posted = send_card(target, view)
            if isawaitable(posted):
                await self._discord(posted)
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    def _post_structure(self, guild: discord.Guild, view: object) -> None:
        submit = getattr(getattr(self._bot, "log_pump", None), "submit", None)
        if not callable(submit):
            return
        channel_id = self._structure_channel_id(guild.id)
        if channel_id is None:
            return
        submit(channel_id, view=view)

    def _structure_channel_id(self, guild_id: int) -> int | None:
        configs = getattr(self._bot, "config_by_guild", None)
        config = configs.get(int(guild_id)) if configs is not None else None
        if config is None:
            return None
        raw = getattr(config, "structure_channel_id", None) or getattr(config, "admin_log_channel_id", None)
        return None if raw is None else int(raw)

    def _log_take(self, guild_id: int, *, previous: int, actor_id: int, room: StoredRoom) -> None:
        configs = getattr(self._bot, "config_by_guild", None)
        config = configs.get(guild_id) if configs is not None else None
        channel_id = getattr(config, "admin_log_channel_id", None)
        submit = getattr(getattr(self._bot, "log_pump", None), "submit", None)
        if not callable(submit):
            return
        submit(
            channel_id,
            view=panel(
                copy.VOICE_STAFF_LOG,
                copy.voice_staff_take(who=f"<@{previous}>", actor=f"<@{actor_id}>", room=f"<#{room.channel_id}>"),
                tone=Tone.WARN,
                footnote=copy.VOICE_LOG_FOOTNOTE,
            ),
        )

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _permission_overwrite(flags: dict[str, bool]) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(**flags)


def _without_user(
    overwrites: dict[object, discord.PermissionOverwrite], user_id: int
) -> dict[object, discord.PermissionOverwrite]:
    return {target: overwrite for target, overwrite in overwrites.items() if getattr(target, "id", None) != user_id}


def _playing_name(member: object) -> str:
    for activity in getattr(member, "activities", ()) or ():
        if getattr(activity, "type", None) == discord.ActivityType.playing:
            name = getattr(activity, "name", None)
            if name:
                return str(name)
    return ""


def occupancy_map(channels: Iterable[object]) -> dict[int, int]:
    found: dict[int, int] = {}
    for channel in channels:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            continue
        found[int(channel_id)] = humans_in(channel)
    return found


def _is_category(channel: object | None) -> bool:
    if channel is None:
        return False
    if isinstance(channel, discord.CategoryChannel):
        return True
    return _channel_kind(channel) == "category"


def _is_voice(channel: object | None) -> bool:
    if channel is None or _is_category(channel):
        return False
    if isinstance(channel, discord.VoiceChannel):
        return True
    kind = _channel_kind(channel)
    return kind in {"", "voice"}


def _channel_kind(channel: object) -> str:
    kind = getattr(channel, "type", None)
    if kind is None:
        return ""
    if kind is discord.ChannelType.category or kind is discord.ChannelType.voice:
        return kind.name
    return str(getattr(kind, "name", kind) or "")


def _live_category_id(channel: object) -> int | None:
    raw = getattr(channel, "category_id", None)
    if raw is not None:
        return int(raw)
    category = getattr(channel, "category", None)
    ident = None if category is None else getattr(category, "id", None)
    return None if ident is None else int(ident)


def _has_flags(perms: object | None, names: tuple[str, ...]) -> bool:
    if perms is None:
        return False
    return all(bool(getattr(perms, name, False)) for name in names)


def _role_can(
    channel: object,
    role: object,
    names: tuple[str, ...],
    *,
    category: object | None,
) -> bool:
    perms = getattr(role, "permissions", None)
    everyone = getattr(getattr(channel, "guild", None), "default_role", None)
    for name in names:
        current = True if perms is None else bool(getattr(perms, name, False))
        if everyone is not None and getattr(everyone, "id", None) != getattr(role, "id", None):
            base = getattr(getattr(everyone, "permissions", None), name, None)
            if isinstance(base, bool):
                current = base or current
        for layer in (category, channel):
            if layer is None:
                continue
            flag = _overwrite_bool(layer, everyone, name)
            if isinstance(flag, bool):
                current = flag
            flag = _overwrite_bool(layer, role, name)
            if isinstance(flag, bool):
                current = flag
        if not current:
            return False
    return True


def _overwrite_bool(layer: object, target: object | None, name: str) -> bool | None:
    if target is None:
        return None
    probe = getattr(layer, "overwrites_for", None)
    overwrite = probe(target) if callable(probe) else None
    if overwrite is None:
        return None
    value = getattr(overwrite, name, None)
    return value if isinstance(value, bool) else None


def _category_is_full(category: object | None) -> bool:
    if category is None:
        return False
    children = getattr(category, "channels", None)
    try:
        return len(children) >= CATEGORY_CHANNEL_CAP if children is not None else False
    except TypeError:
        return False


def _primary_category(guild: object, hub: object) -> object | None:
    category_id = getattr(hub, "category_id", None)
    getter = getattr(guild, "get_channel", None)
    category = getter(category_id) if callable(getter) and category_id is not None else None
    if category is not None and not _is_category(category):
        category = None
    if category is None:
        live = getter(int(hub.channel_id)) if callable(getter) else None
        category = None if live is None else getattr(live, "category", None)
        if category is not None and not _is_category(category):
            category = None
    return category


def _hub_categories(guild: object, hub: object, settings: HubSettings) -> list[object]:
    found: list[object] = []
    seen: set[int] = set()
    primary = _primary_category(guild, hub)
    if primary is not None:
        found.append(primary)
        ident = getattr(primary, "id", None)
        if ident is not None:
            seen.add(int(ident))
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return found
    for cat_id in settings.extra_category_ids:
        if cat_id in seen:
            continue
        category = getter(cat_id)
        if not _is_category(category):
            continue
        found.append(category)
        seen.add(int(cat_id))
    return found


def _first_open_category(guild: object, hub: object, settings: HubSettings) -> object | None:
    for category in _hub_categories(guild, hub, settings):
        if not _category_is_full(category):
            return category
    return None


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
    return count >= GUILD_CHANNEL_CAP


def _is_administrator(member: object) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(getattr(perms, "administrator", False))
