"""Канал статистики: сборка из пульса и stats.py, правка только при смене хеша."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
import structlog
from sqlalchemy import select

from conductor.db.models import Panel, StatsBoard
from conductor.db.session import session_scope
from conductor.domain.access import STAFF_RANKS
from conductor.domain.activity import as_utc
from conductor.domain.pulse import PulseSnapshot
from conductor.domain.stats_board import (
    BoardInputs,
    ChannelCount,
    RankCount,
    RoleCount,
    card_fingerprint,
    dump_role_ids,
    parse_role_ids,
)
from conductor.presentation.layout import edit_card, send_card
from conductor.presentation.persistent import PANEL_LIVE
from conductor.presentation.stats_board import PANEL_KIND, board_card
from conductor.services.access import cached_snapshot
from conductor.services.panels import find_live_kind, find_usable_kind, mark_live, mark_stale
from conductor.services.runtime import log_tick
from conductor.services.stats import top_channels, voice_seconds

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.stats_board")

PERIOD_SECONDS = 600.0
EDIT_GAP = 2.0

__all__ = ["EDIT_GAP", "PERIOD_SECONDS", "StatsBoardService"]


class StatsBoardService:
    def __init__(self, bot: Conductor, *, clock: Any | None = None) -> None:
        self._bot = bot
        self._clock = clock or time.monotonic
        self._last_edit: dict[int, float] = {}
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(PERIOD_SECONDS, "stats-board", self.refresh_all)

    async def card_status(self, guild_id: int) -> str:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(StatsBoard, int(guild_id))
            live = await find_live_kind(session, guild_id=int(guild_id), kind=PANEL_KIND)
        if row is None or not row.channel_id:
            return "off"
        if not row.message_id and not row.panel_id:
            return "off"
        return "live" if live is not None else "stale"

    async def refresh_all(self) -> None:
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(StatsBoard))).all()
            guild_ids = [int(row.guild_id) for row in rows if row.channel_id]
        changed = 0
        for guild_id in guild_ids:
            if not getattr(self._bot, "serves", lambda _gid: True)(guild_id):
                continue
            guild = self._guild(guild_id)
            if guild is None:
                continue
            if await self.refresh(guild, force=False):
                changed += 1
        log_tick(log, "stats_board", changed=changed)

    async def refresh(self, guild: object, *, force: bool = False, now: datetime | None = None) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(StatsBoard, guild_id)
            if row is None or not row.channel_id:
                return False
            role_ids = parse_role_ids(row.roles_json)
            stored_hash = str(row.hash or "")
            live = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
            if live is None or int(live.message_id) <= 0:
                return False
            panel_id = int(live.id)
            channel_id = int(live.channel_id)
            message_id = int(live.message_id)
        inputs = await self._inputs(guild, role_ids, moment)
        digest = card_fingerprint(inputs)
        if not force and stored_hash == digest:
            return False
        if not force and self._recent(guild_id):
            return False
        view = board_card(inputs, updated_at=moment)
        edited = await self._edit_message(guild, channel_id, message_id, view)
        if not edited:
            return False
        self._last_edit[guild_id] = float(self._clock())
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(StatsBoard, guild_id)
            if row is not None:
                row.hash = digest
                row.updated_at = moment
                row.message_id = message_id
            await mark_live(session, [panel_id])
        return True

    async def post(self, guild: object, channel: object, *, actor_id: int, now: datetime | None = None) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, guild_id)
            role_ids = parse_role_ids(row.roles_json)
            existing = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
            if existing is not None and int(existing.channel_id) == channel_id:
                panel_id = int(existing.id)
                message_id = int(existing.message_id)
            else:
                if existing is not None:
                    await mark_stale(session, [int(existing.id)])
                stored = Panel(
                    kind=PANEL_KIND,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=0,
                    ref=str(guild_id),
                    created_by=int(actor_id),
                    created_at=moment,
                    state=PANEL_LIVE,
                )
                session.add(stored)
                await session.flush()
                stored.ref = str(stored.id)
                stored.message_id = -int(stored.id)
                panel_id = int(stored.id)
                message_id = 0
            row.channel_id = channel_id
            row.panel_id = panel_id
        inputs = await self._inputs(guild, role_ids, moment)
        digest = card_fingerprint(inputs)
        view = board_card(inputs, updated_at=moment)
        if message_id > 0:
            edited = await self._edit_message(guild, channel_id, message_id, view)
            if edited:
                await self._remember(guild_id, digest, moment, channel_id, message_id, panel_id)
                return True
        send = getattr(channel, "send", None)
        if not callable(send):
            return False
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("stats_board_send_failed", guild_id=guild_id, error=str(exc))
            if message_id <= 0:
                # Заглушка с отрицательным message_id иначе останется «живой» и refresh будет вечно молчать.
                async with session_scope(self._bot.sessions) as session:
                    await mark_stale(session, [panel_id])
            return False
        await self._remember(guild_id, digest, moment, channel_id, int(message.id), panel_id)
        return True

    async def set_channel(self, guild_id: int, channel_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, int(guild_id))
            before = row.channel_id
            row.channel_id = int(channel_id)
        return None if before is None else int(before)

    async def set_roles(self, guild_id: int, role_ids: object) -> tuple[int, ...]:
        wanted = parse_role_ids(role_ids)
        async with session_scope(self._bot.sessions) as session:
            row = await self._row(session, int(guild_id))
            before = parse_role_ids(row.roles_json)
            row.roles_json = dump_role_ids(wanted)
        return before

    async def _inputs(self, guild: object, role_ids: tuple[int, ...], now: datetime) -> BoardInputs:
        snap = await self._pulse(guild, now)
        ranks = await self._ranks(guild)
        roles = tuple(RoleCount(role_id, _count_role(guild, role_id)) for role_id in role_ids)
        async with session_scope(self._bot.sessions) as session:
            channel_rows = await top_channels(session, int(guild.id), days=7, now=now)
            voice = await voice_seconds(session, int(guild.id), days=7, now=now)
        channels = tuple(ChannelCount(channel_id, messages, authors) for channel_id, messages, authors in channel_rows)
        if snap is None:
            return BoardInputs(ranks=ranks, roles=roles, channels=channels, voice_seconds=voice)
        return BoardInputs(
            ranks=ranks,
            duty=int(snap.inputs.duty_watchers),
            humans=int(snap.inputs.humans),
            bots=int(snap.inputs.bots),
            boosters=int(snap.inputs.boosters),
            roles=roles,
            channels=channels,
            voice_seconds=voice,
            tickets_open=int(snap.inputs.tickets_open),
            gathering_title=snap.inputs.next_gathering_title,
            gathering_at=snap.inputs.next_gathering_at,
            gathering_id=snap.inputs.next_gathering_id,
        )

    async def _pulse(self, guild: object, now: datetime) -> PulseSnapshot | None:
        pulse = getattr(self._bot, "pulse", None)
        snapshot = getattr(pulse, "snapshot", None)
        if not callable(snapshot):
            return None
        result = await snapshot(guild, now=now)
        return result if isinstance(result, PulseSnapshot) else None

    async def _ranks(self, guild: object) -> tuple[RankCount, ...]:
        access = await cached_snapshot(self._bot, int(guild.id))
        found: list[RankCount] = []
        for rank in STAFF_RANKS:
            found.append(RankCount(rank.key, _count_any_role(guild, access.rank_roles.get(rank, ()))))
        return tuple(found)

    async def _remember(
        self,
        guild_id: int,
        digest: str,
        now: datetime,
        channel_id: int,
        message_id: int,
        panel_id: int,
    ) -> None:
        self._last_edit[int(guild_id)] = float(self._clock())
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(StatsBoard, int(guild_id))
            if row is not None:
                row.hash = digest
                row.updated_at = now
                row.channel_id = int(channel_id)
                row.message_id = int(message_id)
                row.panel_id = int(panel_id)
            panel = await session.get(Panel, int(panel_id))
            if panel is not None:
                panel.message_id = int(message_id)
                panel.channel_id = int(channel_id)
                panel.ref = str(guild_id)
                panel.state = PANEL_LIVE

    async def _row(self, session: Any, guild_id: int) -> StatsBoard:
        row = await session.get(StatsBoard, int(guild_id))
        if row is None:
            row = StatsBoard(guild_id=int(guild_id), roles_json="[]", hash="")
            session.add(row)
            await session.flush()
        return row

    def _recent(self, guild_id: int) -> bool:
        last = self._last_edit.get(int(guild_id))
        if last is None:
            return False
        return float(self._clock()) - last < EDIT_GAP

    def _guild(self, guild_id: int) -> Any | None:
        getter = getattr(self._bot, "get_guild", None)
        if callable(getter):
            return getter(int(guild_id))
        for guild in getattr(self._bot, "guilds", ()) or ():
            if int(getattr(guild, "id", 0) or 0) == int(guild_id):
                return guild
        return None

    async def _edit_message(self, guild: object, channel_id: int, message_id: int, view: Any) -> bool:
        channel = _channel(guild, int(channel_id))
        if channel is None:
            return False
        fetch = getattr(channel, "fetch_message", None)
        target = None
        if callable(fetch):
            try:
                target = await self._discord(fetch(int(message_id)))
            except discord.NotFound:
                await self._mark_gone(int(getattr(guild, "id", 0) or 0))
                return False
            except discord.HTTPException:
                return False
        if target is None:
            getter = getattr(channel, "get_partial_message", None)
            target = getter(int(message_id)) if callable(getter) else None
        edit = getattr(target, "edit", None)
        if not callable(edit):
            return False
        try:
            await self._discord(edit_card(target, view))
        except discord.NotFound:
            await self._mark_gone(int(getattr(guild, "id", 0) or 0))
            return False
        except discord.HTTPException as exc:
            log.warning("stats_board_edit_failed", channel_id=channel_id, error=str(exc))
            return False
        return True

    async def _mark_gone(self, guild_id: int) -> None:
        if not guild_id:
            return
        async with session_scope(self._bot.sessions) as session:
            live = await find_live_kind(session, guild_id=int(guild_id), kind=PANEL_KIND)
            if live is not None:
                await mark_stale(session, [int(live.id)])

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _channel(guild: object, channel_id: int) -> Any:
    getter = getattr(guild, "get_channel", None)
    if callable(getter):
        return getter(int(channel_id))
    return None


def _count_any_role(guild: object, role_ids: object) -> int:
    wanted = {int(role_id) for role_id in role_ids or ()}
    if not wanted:
        return 0
    seen: set[int] = set()
    for role_id in wanted:
        role = _role(guild, role_id)
        members = getattr(role, "members", None) if role is not None else None
        if members is None:
            continue
        for member in members:
            member_id = int(getattr(member, "id", 0) or 0)
            if member_id:
                seen.add(member_id)
    if seen:
        return len(seen)
    have = 0
    for member in getattr(guild, "members", ()) or ():
        roles = getattr(member, "roles", ()) or ()
        if any(int(getattr(role, "id", 0) or 0) in wanted for role in roles):
            have += 1
    return have


def _count_role(guild: object, role_id: int) -> int:
    return _count_any_role(guild, (role_id,))


def _role(guild: object, role_id: int) -> Any:
    getter = getattr(guild, "get_role", None)
    if callable(getter):
        return getter(int(role_id))
    return None
