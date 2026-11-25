"""Набор в #поиск-команды: карточка, пинги, комната, срок."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import func, select

from conductor.db.models import LfgMember, LfgPost, LfgSettings, VoiceRoom
from conductor.db.session import session_scope
from conductor.domain.access import MOD_PURGE
from conductor.domain.activity import as_utc
from conductor.domain.lfg import (
    AUTHOR_WINDOW,
    LFG_ROLE_COOLDOWN,
    PING_COOLDOWN_DEFAULT,
    TTL_DEFAULT,
    USER_COOLDOWN_DEFAULT,
    CloseReason,
    CloseVerdict,
    JoinVerdict,
    LeaveVerdict,
    LfgCard,
    LfgSettingsView,
    PostVerdict,
    can_post,
    card_hash,
    check_note,
    clamp_settings,
    close_verdict,
    is_full,
    is_live,
    join_verdict,
    leave_verdict,
    parse_need,
    ping_ready,
    playable_games,
    room_limit,
    room_name,
    should_attach_room,
)
from conductor.domain.rooms import created_channel_id, room_for_owner
from conductor.domain.self_roles import OptionTag
from conductor.domain.staff_feed import ACTION_LFG_CLOSE
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import denied, edit_card, respond, send_card
from conductor.presentation.lfg import (
    PANEL_KIND,
    LfgModal,
    closed_view,
    game_pick_view,
    live_view,
    post_footnote,
    starter_view,
)
from conductor.presentation.mentions import role_mentions
from conductor.services.panels import find_live_kind, find_usable_kind, mark_live, mark_stale, record_panel
from conductor.services.runtime import log_tick
from conductor.services.staff_feed import actor_rank_of, emit_staff

log = structlog.get_logger("conductor.lfg")

SWEEP_SECONDS = 300.0

__all__ = ["SWEEP_SECONDS", "LfgService"]


@dataclass(frozen=True, slots=True)
class LfgAction:
    error: str = ""
    note_reason: str = ""
    post_id: int | None = None
    game_pinged: bool = False
    lfg_pinged: bool = False
    room_id: int | None = None
    room_mention: str | None = None


class LfgService:
    def __init__(self, bot: Any, *, clock: Any | None = None) -> None:
        self._bot = bot
        self._clock = clock or (lambda: datetime.now(UTC))
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(SWEEP_SECONDS, "lfg-sweep", self.sweep)

    def _now(self) -> datetime:
        return as_utc(self._clock())

    async def settings(self, guild_id: int) -> LfgSettingsView:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgSettings, int(guild_id))
        return _view(row, guild_id)

    async def set_channel(self, guild_id: int, channel_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, int(guild_id))
            before = None if row.channel_id is None else int(row.channel_id)
            row.channel_id = int(channel_id)
        return before

    async def set_numbers(self, guild_id: int, ttl: int, ping: int, user: int) -> tuple[int, int, int]:
        ttl, ping, user = clamp_settings(ttl, ping, user)
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, int(guild_id))
            before = (int(row.ttl_minutes), int(row.ping_cooldown_minutes), int(row.per_user_cooldown_minutes))
            row.ttl_minutes = ttl
            row.ping_cooldown_minutes = ping
            row.per_user_cooldown_minutes = user
        return before

    async def card_status(self, guild_id: int) -> str:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgSettings, int(guild_id))
            live = await find_live_kind(session, guild_id=int(guild_id), kind=PANEL_KIND)
        if row is None or not row.channel_id:
            return "off"
        return "live" if live is not None else "stale"

    async def guard_panel(self, guild_id: int, ref: str) -> GuardResource | None:
        """Стартовая карточка: ref — id гильдии. Нажатие само доказывает, что сообщение живо."""
        if str(int(guild_id)) != str(ref):
            return GuardResource(ref=str(ref), guild_id=int(guild_id), closed=True)
        return GuardResource(ref=str(ref), guild_id=int(guild_id))

    async def guard_post(self, guild_id: int, ref: str) -> GuardResource | None:
        """Кнопки набора: ресурс — сам пост, закрытый пост даёт «устарело»."""
        try:
            post_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, post_id)
            if row is None:
                return None
            return GuardResource(
                ref=str(ref),
                guild_id=int(row.guild_id),
                owner_id=int(row.author_id),
                closed=row.closed_at is not None,
            )

    async def channel_mention(self, guild_id: int) -> str | None:
        settings = await self.settings(int(guild_id))
        if not settings.channel_id:
            return None
        return f"<#{int(settings.channel_id)}>"

    async def author_count(self, guild_id: int, user_id: int, *, days: int = 30, now: datetime | None = None) -> int:
        moment = as_utc(now or self._now())
        since = moment - (AUTHOR_WINDOW if days == 30 else timedelta(days=int(days)))
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(LfgPost.id).where(
                        LfgPost.guild_id == int(guild_id),
                        LfgPost.author_id == int(user_id),
                        LfgPost.created_at >= since,
                    )
                )
            ).all()
        return len(rows)

    async def playable(self, guild_id: int, role_ids: list[int] | tuple[int, ...]) -> tuple[tuple[int, str], ...]:
        options = await self._game_options(int(guild_id))
        return playable_games(options, role_ids)

    async def room_line(self, guild_id: int, room_id: int) -> str | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(LfgPost)
                .where(
                    LfgPost.guild_id == int(guild_id),
                    LfgPost.room_id == int(room_id),
                    LfgPost.closed_at.is_(None),
                )
                .order_by(LfgPost.created_at.desc())
            )
            if row is None:
                return None
            signed = await self._count_signed(session, row.id)
            game_role_id = int(row.game_role_id)
            need = int(row.need)
        guild = self._guild(int(guild_id))
        role = None if guild is None else guild.get_role(game_role_id)
        game = str(getattr(role, "name", None) or copy.GAME_FALLBACK)
        return copy.lfg_room_line(game, signed, need)

    async def post_starter(self, guild: object, channel: object, *, actor_id: int) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        send = getattr(channel, "send", None)
        if not callable(send) or not channel_id:
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, guild_id)
            row.channel_id = channel_id
            existing = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
            same = existing is not None and int(existing.channel_id) == channel_id
            message_id = int(existing.message_id) if same and existing is not None else 0
            panel_id = int(existing.id) if existing is not None else 0
            if existing is not None and not same:
                await mark_stale(session, [int(existing.id)])
        view = starter_view(guild_id)
        if message_id:
            edited = await self._edit_message(guild, channel_id, message_id, view)
            if edited:
                if panel_id:
                    async with session_scope(self._bot.sessions) as session:
                        await mark_live(session, [panel_id])
                return True
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("lfg_starter_failed", guild_id=guild_id, error=str(exc))
            return False
        async with session_scope(self._bot.sessions) as session:
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=int(message.id),
                ref=str(guild_id),
                created_by=int(actor_id),
                now=self._now(),
            )
        return True

    async def begin(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await respond(interaction, denied(copy.LFG_NO_CHANNEL))
            return
        games = await self.playable(guild.id, [int(role.id) for role in member.roles])
        if not games:
            await respond(interaction, denied(copy.lfg_no_roles(await self._roles_mention(guild.id))))
            return
        if len(games) == 1:
            await interaction.response.send_modal(LfgModal(games[0][0]))
            return

        async def on_pick(press: discord.Interaction, role_id: int) -> None:
            held = {int(role.id) for role in getattr(press.user, "roles", ()) or ()}
            if role_id not in held or role_id not in {item[0] for item in games}:
                await respond(press, denied(copy.LFG_NO_GAME))
                return
            await press.response.send_modal(LfgModal(role_id))

        await respond(interaction, game_pick_view(games, on_pick))

    async def publish(
        self,
        guild: discord.Guild,
        author: discord.Member,
        *,
        game_role_id: int,
        need: str | int,
        note: str,
    ) -> LfgAction:
        settings = await self.settings(guild.id)
        if not settings.channel_id:
            return LfgAction(error=PostVerdict.NO_CHANNEL.value)
        parsed = parse_need(str(need))
        if parsed is None:
            return LfgAction(error=PostVerdict.BAD_NEED.value)
        ok, cleaned, reason = check_note(note)
        if not ok:
            return LfgAction(error=PostVerdict.BAD_NOTE.value, note_reason=reason)
        games = await self.playable(guild.id, [int(role.id) for role in author.roles])
        labels = {role_id: label for role_id, label in games}
        if int(game_role_id) not in labels:
            return LfgAction(error=PostVerdict.NO_GAME.value)
        moment = self._now()
        async with session_scope(self._bot.sessions) as session:
            live = await session.scalar(
                select(LfgPost.id).where(
                    LfgPost.guild_id == guild.id,
                    LfgPost.author_id == author.id,
                    LfgPost.closed_at.is_(None),
                )
            )
            last = await session.scalar(
                select(LfgPost.created_at)
                .where(LfgPost.guild_id == guild.id, LfgPost.author_id == author.id)
                .order_by(LfgPost.created_at.desc())
                .limit(1)
            )
            verdict = can_post(
                has_live=live is not None,
                last_created_at=last,
                now=moment,
                cooldown=timedelta(minutes=settings.per_user_cooldown_minutes),
            )
            if verdict is not PostVerdict.OK:
                return LfgAction(error=verdict.value)
            last_game = await session.scalar(
                select(LfgPost.created_at)
                .where(
                    LfgPost.guild_id == guild.id,
                    LfgPost.game_role_id == int(game_role_id),
                    LfgPost.game_pinged.is_(True),
                )
                .order_by(LfgPost.created_at.desc())
                .limit(1)
            )
            last_lfg = await session.scalar(
                select(LfgPost.created_at)
                .where(LfgPost.guild_id == guild.id, LfgPost.lfg_pinged.is_(True))
                .order_by(LfgPost.created_at.desc())
                .limit(1)
            )
        game_pinged = ping_ready(
            last_at=last_game,
            now=moment,
            cooldown=timedelta(minutes=settings.ping_cooldown_minutes),
        )
        lfg_role_id = await self._lfg_role(guild.id)
        lfg_pinged = bool(lfg_role_id) and ping_ready(last_at=last_lfg, now=moment, cooldown=LFG_ROLE_COOLDOWN)
        expires = moment + timedelta(minutes=settings.ttl_minutes)
        owns, in_hub, in_room = await self._voice_flags(author)
        attach = should_attach_room(owns_room=owns, in_hub=in_hub, in_room=in_room)
        room_id, room_channel_id = await self._attach_room(
            guild, author, game=labels[int(game_role_id)], need=parsed, wanted=attach
        )
        channel = guild.get_channel(int(settings.channel_id))
        send = getattr(channel, "send", None)
        if not callable(send):
            return LfgAction(error=PostVerdict.NO_CHANNEL.value)
        async with session_scope(self._bot.sessions) as session:
            row = LfgPost(
                guild_id=guild.id,
                channel_id=int(settings.channel_id),
                message_id=None,
                author_id=author.id,
                game_role_id=int(game_role_id),
                need=parsed,
                note=cleaned,
                room_id=room_id,
                created_at=moment,
                expires_at=expires,
                game_pinged=game_pinged,
                lfg_pinged=lfg_pinged,
            )
            session.add(row)
            await session.flush()
            post_id = int(row.id)
        card = await self._card(
            guild,
            post_id=post_id,
            game=labels[int(game_role_id)],
            author_id=author.id,
            need=parsed,
            signed=0,
            note=cleaned,
            expires_at=expires,
            room_channel_id=room_channel_id,
            signed_ids=(),
            game_pinged=game_pinged,
            lfg_pinged=lfg_pinged,
            game_role_id=int(game_role_id),
            lfg_role_id=lfg_role_id,
        )
        ping_ids = []
        if game_pinged:
            ping_ids.append(int(game_role_id))
        if lfg_pinged and lfg_role_id:
            ping_ids.append(int(lfg_role_id))
        try:
            message = await self._discord(send_card(channel, live_view(card), mentions=role_mentions(*ping_ids)))
        except discord.HTTPException as exc:
            log.warning("lfg_post_failed", guild_id=guild.id, error=str(exc))
            async with session_scope(self._bot.sessions) as session:
                stale = await session.get(LfgPost, post_id)
                if stale is not None:
                    await session.delete(stale)
            return LfgAction(error=PostVerdict.NO_CHANNEL.value)
        async with session_scope(self._bot.sessions) as session:
            stored = await session.get(LfgPost, post_id)
            if stored is not None:
                stored.message_id = int(message.id)
        return LfgAction(
            post_id=post_id,
            game_pinged=game_pinged,
            lfg_pinged=lfg_pinged,
            room_id=room_id,
        )

    async def join(self, guild: discord.Guild, post_id: int, member: discord.Member) -> LfgAction:
        moment = self._now()
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None or row.guild_id != guild.id:
                return LfgAction(error=JoinVerdict.CLOSED.value)
            already = await session.get(LfgMember, (row.id, member.id))
            signed = await self._count_signed(session, row.id)
            verdict = join_verdict(
                closed=not is_live(closed_at=row.closed_at, expires_at=row.expires_at, now=moment),
                is_author=member.id == row.author_id,
                already=already is not None,
                signed=signed,
                need=int(row.need),
            )
            if verdict is not JoinVerdict.OK:
                return LfgAction(error=verdict.value)
            session.add(LfgMember(post_id=row.id, user_id=member.id, joined_at=moment))
            signed += 1
            if is_full(int(row.need), signed):
                row.closed_at = moment
                row.closed_reason = CloseReason.FULL.value
            snapshot = _snapshot(row)
            snapshot_signed = signed
        room_id, room_channel_id = await self._ensure_room(guild, snapshot, snapshot_signed)
        if room_id:
            rooms = getattr(self._bot, "rooms", None)
            permit = getattr(rooms, "permit_member", None)
            if callable(permit):
                await permit(guild, room_id, member)
            refresh = getattr(rooms, "refresh_panel", None)
            if callable(refresh):
                await refresh(guild, room_id)
        await self._queue_refresh(guild, snapshot.id)
        mention = f"<#{room_channel_id}>" if room_channel_id else None
        return LfgAction(post_id=snapshot.id, room_id=room_id, room_mention=mention)

    async def leave(self, guild: discord.Guild, post_id: int, member: discord.Member) -> LfgAction:
        moment = self._now()
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None or row.guild_id != guild.id:
                return LfgAction(error=LeaveVerdict.CLOSED.value)
            inside = await session.get(LfgMember, (row.id, member.id))
            verdict = leave_verdict(
                closed=not is_live(closed_at=row.closed_at, expires_at=row.expires_at, now=moment),
                is_author=member.id == row.author_id,
                inside=inside is not None,
            )
            if verdict is not LeaveVerdict.OK:
                return LfgAction(error=verdict.value)
            await session.delete(inside)
            room_id = None if row.room_id is None else int(row.room_id)
        await self._queue_refresh(guild, int(post_id))
        if room_id:
            rooms = getattr(self._bot, "rooms", None)
            refresh = getattr(rooms, "refresh_panel", None)
            if callable(refresh):
                await refresh(guild, room_id)
        return LfgAction(post_id=int(post_id))

    async def signed_ids(self, guild: discord.Guild, post_id: int) -> list[int]:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None or row.guild_id != guild.id:
                return []
            rows = (
                await session.scalars(
                    select(LfgMember.user_id)
                    .where(LfgMember.post_id == row.id)
                    .order_by(LfgMember.joined_at, LfgMember.user_id)
                )
            ).all()
        return [int(item) for item in rows]

    async def close(self, guild: discord.Guild, post_id: int, member: discord.Member) -> LfgAction:
        moment = self._now()
        is_staff = await self._is_staff(guild, member)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None or row.guild_id != guild.id:
                return LfgAction(error=CloseVerdict.CLOSED.value)
            verdict = close_verdict(
                closed=row.closed_at is not None,
                is_author=member.id == row.author_id,
                is_staff=is_staff,
            )
            if verdict is not CloseVerdict.OK:
                return LfgAction(error=verdict.value)
            reason = CloseReason.STAFF if is_staff and member.id != row.author_id else CloseReason.AUTHOR
            row.closed_at = moment
            row.closed_reason = reason.value
            author_id = int(row.author_id)
            room_id = None if row.room_id is None else int(row.room_id)
            game_role_id = int(row.game_role_id)
        if reason is CloseReason.STAFF:
            rank = await actor_rank_of(self._bot, guild, member.id)
            role = guild.get_role(game_role_id)
            await emit_staff(
                self._bot,
                guild.id,
                actor_id=member.id,
                actor_rank=rank,
                action=ACTION_LFG_CLOSE,
                target=author_id,
                ref=int(post_id),
                details=None if role is None else str(role.name),
            )
        await self._queue_refresh(guild, int(post_id))
        if room_id:
            rooms = getattr(self._bot, "rooms", None)
            refresh = getattr(rooms, "refresh_panel", None)
            if callable(refresh):
                await refresh(guild, room_id)
        return LfgAction(post_id=int(post_id))

    async def room_hint(self, guild: discord.Guild, post_id: int) -> str | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None or row.guild_id != guild.id or row.room_id is None:
                return None
            room_id = int(row.room_id)
        rooms = getattr(self._bot, "rooms", None)
        lookup = getattr(rooms, "room_by_id", None)
        channel_id = None
        if callable(lookup):
            stored = await lookup(guild.id, room_id)
            channel_id = None if stored is None else int(getattr(stored, "channel_id", 0) or 0)
        if not channel_id:
            async with session_scope(self._bot.sessions) as session:
                room = await session.get(VoiceRoom, room_id)
                if room is not None and room.guild_id == guild.id:
                    channel_id = int(room.channel_id)
        if not channel_id:
            return None
        return copy.LFG_ROOM_HINT.format(channel=f"<#{channel_id}>")

    async def sweep(self, *, now: datetime | None = None) -> None:
        moment = as_utc(now) if now is not None else self._now()
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(select(LfgPost).where(LfgPost.closed_at.is_(None), LfgPost.expires_at <= moment))
            ).all()
            expired = [
                (int(row.guild_id), int(row.id), None if row.room_id is None else int(row.room_id)) for row in rows
            ]
            for row in rows:
                row.closed_at = moment
                row.closed_reason = CloseReason.EXPIRED.value
        for guild_id, post_id, room_id in expired:
            guild = self._guild(guild_id)
            if guild is None:
                continue
            await self._refresh_card(guild, post_id)
            if room_id:
                rooms = getattr(self._bot, "rooms", None)
                refresh = getattr(rooms, "refresh_panel", None)
                if callable(refresh):
                    await refresh(guild, room_id)
        log_tick(log, "lfg_sweep", changed=len(expired))

    async def _ensure_room(self, guild: discord.Guild, post: _PostSnap, _signed: int) -> tuple[int | None, int | None]:
        if post.room_id:
            channel_id = await self._room_channel(guild.id, post.room_id)
            return post.room_id, channel_id
        author = guild.get_member(post.author_id)
        if author is None:
            return None, None
        role = guild.get_role(post.game_role_id)
        game = str(getattr(role, "name", None) or copy.GAME_FALLBACK)
        room_id, channel_id = await self._attach_room(guild, author, game=game, need=post.need, wanted=True)
        if room_id:
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(LfgPost, post.id)
                if row is not None:
                    row.room_id = room_id
        return room_id, channel_id

    async def _attach_room(
        self,
        guild: discord.Guild,
        author: discord.Member,
        *,
        game: str,
        need: int,
        wanted: bool,
    ) -> tuple[int | None, int | None]:
        if not wanted:
            return None, None
        rooms = getattr(self._bot, "rooms", None)
        create = getattr(rooms, "create_for", None)
        if not callable(create):
            return None, None
        channel_id = created_channel_id(
            await create(author, name=room_name(game, author.display_name), user_limit=room_limit(need))
        )
        if channel_id is None:
            return None, None
        stored = await rooms.room_by_channel(guild.id, int(channel_id))
        if stored is None:
            return None, int(channel_id)
        return int(stored.id), int(channel_id)

    async def _voice_flags(self, member: discord.Member) -> tuple[bool, bool, bool]:
        rooms = getattr(self._bot, "rooms", None)
        if rooms is None:
            return False, False, False
        guild = member.guild
        owns = False
        in_hub = False
        in_room = False
        hub_for = getattr(rooms, "hub_for", None)
        rooms_for = getattr(rooms, "rooms_for", None)
        if callable(hub_for) and callable(rooms_for):
            async with session_scope(self._bot.sessions) as session:
                hub = await hub_for(session, guild.id)
                stored = await rooms_for(session, guild.id)
            owns = room_for_owner(stored, member.id) is not None
            voice = getattr(getattr(member, "voice", None), "channel", None)
            voice_id = None if voice is None else int(getattr(voice, "id", 0) or 0)
            if voice_id and hub is not None and int(getattr(hub, "channel_id", 0) or 0) == voice_id:
                in_hub = True
            if voice_id:
                found = await rooms.room_by_channel(guild.id, voice_id)
                in_room = found is not None
        return owns, in_hub, in_room

    async def _refresh_card(self, guild: discord.Guild, post_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(LfgPost, int(post_id))
            if row is None:
                return
            snap = _snapshot(row)
            signed = await self._count_signed(session, row.id)
            signed_ids = await self._preview_ids(session, row.id)
        lfg_role_id = await self._lfg_role(guild.id)
        role = guild.get_role(snap.game_role_id)
        game = str(getattr(role, "name", None) or copy.GAME_FALLBACK)
        room_channel_id = await self._room_channel(guild.id, snap.room_id) if snap.room_id else None
        card = await self._card(
            guild,
            post_id=snap.id,
            game=game,
            author_id=snap.author_id,
            need=snap.need,
            signed=signed,
            note=snap.note,
            expires_at=snap.expires_at,
            room_channel_id=room_channel_id,
            signed_ids=signed_ids,
            game_pinged=snap.game_pinged,
            lfg_pinged=snap.lfg_pinged,
            game_role_id=snap.game_role_id,
            lfg_role_id=lfg_role_id,
            closed_reason=snap.closed_reason,
        )
        view = closed_view(card) if snap.closed_at is not None else live_view(card)
        if not snap.message_id:
            return
        digest = card_hash(
            game=card.game,
            author_id=card.author_id,
            need=card.need,
            signed=card.signed,
            note=card.note,
            expires_at=card.expires_at,
            room_channel_id=card.room_channel_id,
            closed_reason=card.closed_reason,
            signed_ids=card.signed_ids,
        )
        _ = digest
        await self._edit_message(guild, snap.channel_id, snap.message_id, view)

    async def _queue_refresh(self, guild: discord.Guild, post_id: int) -> None:
        cards = getattr(self._bot, "cards", None)
        request = getattr(cards, "request", None)
        if callable(request):
            request(("lfg", int(guild.id), int(post_id)), lambda: self._refresh_card(guild, post_id))
            return
        await self._refresh_card(guild, post_id)

    async def _card(
        self,
        guild: discord.Guild,
        *,
        post_id: int,
        game: str,
        author_id: int,
        need: int,
        signed: int,
        note: str,
        expires_at: datetime,
        room_channel_id: int | None,
        signed_ids: tuple[int, ...],
        game_pinged: bool,
        lfg_pinged: bool,
        game_role_id: int,
        lfg_role_id: int | None,
        closed_reason: str | None = None,
    ) -> LfgCard:
        _ = guild
        footnote = ""
        if closed_reason is None:
            footnote = post_footnote(
                expires_text=discord.utils.format_dt(as_utc(expires_at), "R"),
                game_role_id=game_role_id,
                lfg_role_id=lfg_role_id,
                game_pinged=game_pinged,
                lfg_pinged=lfg_pinged,
            )
        return LfgCard(
            post_id=post_id,
            game=game,
            author_id=author_id,
            need=need,
            signed=signed,
            note=note,
            expires_at=expires_at,
            room_channel_id=room_channel_id,
            closed_reason=closed_reason,
            signed_ids=signed_ids,
            game_pinged=game_pinged,
            lfg_pinged=lfg_pinged,
            footnote=footnote,
        )

    async def _game_options(self, guild_id: int) -> tuple[tuple[int, str], ...]:
        getter = getattr(getattr(self._bot, "self_roles", None), "game_options", None)
        if not callable(getter):
            return ()
        options = await getter(int(guild_id))
        return tuple((int(item.role_id), str(item.label)) for item in options)

    async def _lfg_role(self, guild_id: int) -> int | None:
        getter = getattr(getattr(self._bot, "self_roles", None), "role_for_tag", None)
        if not callable(getter):
            return None
        role_id = await getter(int(guild_id), OptionTag.LFG.value)
        return None if not role_id else int(role_id)

    async def _roles_mention(self, guild_id: int) -> str | None:
        menus = getattr(getattr(self._bot, "self_roles", None), "menus", None)
        if not callable(menus):
            return None
        for menu in await menus(int(guild_id)):
            channel_id = getattr(menu, "channel_id", None)
            if channel_id:
                return f"<#{int(channel_id)}>"
        return None

    async def _is_staff(self, guild: discord.Guild, member: discord.Member) -> bool:
        method = getattr(self._bot, "decide_access", None)
        if not callable(method):
            return bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))
        decision = await method(
            guild_id=guild.id,
            user_id=member.id,
            member_role_ids=tuple(int(role.id) for role in getattr(member, "roles", ()) or ()),
            is_administrator=bool(getattr(getattr(member, "guild_permissions", None), "administrator", False)),
            key=MOD_PURGE,
        )
        return bool(getattr(decision, "allowed", False) or getattr(decision, "developer", False))

    async def _room_channel(self, guild_id: int, room_id: int | None) -> int | None:
        if not room_id:
            return None
        async with session_scope(self._bot.sessions) as session:
            room = await session.get(VoiceRoom, int(room_id))
            if room is None or room.guild_id != int(guild_id):
                return None
            return int(room.channel_id)

    async def _edit_message(self, guild: object, channel_id: int, message_id: int, view: object) -> bool:
        getter = getattr(guild, "get_channel", None)
        channel = getter(int(channel_id)) if callable(getter) else None
        fetch = getattr(channel, "fetch_message", None)
        if not callable(fetch):
            return False
        try:
            message = await fetch(int(message_id))
            await edit_card(message, view)
        except discord.HTTPException as exc:
            log.warning("lfg_edit_failed", guild_id=getattr(guild, "id", 0), error=str(exc))
            return False
        return True

    async def _row(self, session: Any, guild_id: int) -> LfgSettings:
        row = await session.get(LfgSettings, int(guild_id))
        if row is None:
            row = LfgSettings(
                guild_id=int(guild_id),
                ttl_minutes=TTL_DEFAULT,
                ping_cooldown_minutes=PING_COOLDOWN_DEFAULT,
                per_user_cooldown_minutes=USER_COOLDOWN_DEFAULT,
            )
            session.add(row)
            await session.flush()
        return row

    def _guild(self, guild_id: int) -> discord.Guild | None:
        getter = getattr(self._bot, "get_guild", None)
        if not callable(getter):
            return None
        guild = getter(int(guild_id))
        return guild if guild is not None else None

    async def _count_signed(self, session: Any, post_id: int) -> int:
        value = await session.scalar(
            select(func.count()).select_from(LfgMember).where(LfgMember.post_id == int(post_id))
        )
        return int(value or 0)

    async def _preview_ids(self, session: Any, post_id: int, *, limit: int = 10) -> tuple[int, ...]:
        rows = (
            await session.scalars(
                select(LfgMember.user_id)
                .where(LfgMember.post_id == int(post_id))
                .order_by(LfgMember.joined_at, LfgMember.user_id)
                .limit(int(limit))
            )
        ).all()
        return tuple(int(item) for item in rows)

    async def _discord(self, awaitable: Any) -> Any:
        return await awaitable


@dataclass(frozen=True, slots=True)
class _PostSnap:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    author_id: int
    game_role_id: int
    need: int
    note: str
    room_id: int | None
    expires_at: datetime
    closed_at: datetime | None
    closed_reason: str | None
    game_pinged: bool
    lfg_pinged: bool


def _snapshot(row: LfgPost) -> _PostSnap:
    return _PostSnap(
        id=int(row.id),
        guild_id=int(row.guild_id),
        channel_id=int(row.channel_id),
        message_id=None if row.message_id is None else int(row.message_id),
        author_id=int(row.author_id),
        game_role_id=int(row.game_role_id),
        need=int(row.need),
        note=str(row.note or ""),
        room_id=None if row.room_id is None else int(row.room_id),
        expires_at=row.expires_at,
        closed_at=row.closed_at,
        closed_reason=None if row.closed_reason is None else str(row.closed_reason),
        game_pinged=bool(row.game_pinged),
        lfg_pinged=bool(row.lfg_pinged),
    )


def _view(row: LfgSettings | None, guild_id: int) -> LfgSettingsView:
    if row is None:
        return LfgSettingsView(guild_id=int(guild_id))
    return LfgSettingsView(
        guild_id=int(row.guild_id),
        channel_id=None if row.channel_id is None else int(row.channel_id),
        ttl_minutes=int(row.ttl_minutes),
        ping_cooldown_minutes=int(row.ping_cooldown_minutes),
        per_user_cooldown_minutes=int(row.per_user_cooldown_minutes),
    )
