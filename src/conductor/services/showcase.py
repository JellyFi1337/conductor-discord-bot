"""Единая отправка и честный статус витрин. Постеры не дублируются."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import discord
import structlog
from sqlalchemy import select

from conductor.db.models import (
    GuildConfig,
    LfgSettings,
    Panel,
    SelfRoleMenu,
    StatsBoard,
    TicketPanel,
    VerificationSettings,
)
from conductor.db.session import session_scope
from conductor.domain.showcase import SHOWCASES, ShowcaseResult, ShowcaseSpec, ShowcaseState, state_of
from conductor.domain.tickets import parse_ticket_settings
from conductor.presentation import copy
from conductor.presentation.persistent import PANEL_LIVE
from conductor.services.guild_config import get_or_create_config
from conductor.services.panels import find_live_kind, mark_stale

log = structlog.get_logger("conductor.showcase")

__all__ = ["ShowcaseService"]

Poster = Callable[[discord.Guild, ShowcaseSpec, Any, int], Awaitable[ShowcaseResult]]


@dataclass(frozen=True, slots=True)
class _LiveRow:
    id: int
    kind: str
    channel_id: int
    message_id: int
    state: str


class ShowcaseService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._posters: Mapping[str, Poster] = {
            "verify": self._post_verify,
            "self_roles": self._post_self_roles,
            "tickets": self._post_tickets,
            "lfg": self._post_lfg,
            "stats": self._post_stats,
            "automod": self._post_automod,
        }

    def invalidate(self, guild_id: int) -> None:
        """Сбросить кэш статусов: каркас гильдии только что изменился."""
        self._drop_cache(int(guild_id))

    async def status(self, guild: discord.Guild, spec: ShowcaseSpec) -> ShowcaseState:
        found = await self.statuses(guild)
        return found.get(spec.id, ShowcaseState.OFF)

    async def statuses(self, guild: discord.Guild) -> dict[str, ShowcaseState]:
        cache = getattr(getattr(self._bot, "caches", None), "showcase", None)
        cached = None if cache is None else cache.get(int(guild.id))
        if cached is not None:
            return {key: ShowcaseState(value) for key, value in cached.items()}
        bound = await self._bound_ids(int(guild.id))
        lives = await self._live_rows(int(guild.id))
        found: dict[str, ShowcaseState] = {}
        for spec in SHOWCASES:
            found[spec.id] = await self._status_one(guild, spec, bound, lives.get(spec.panel_kind))
        if cache is not None:
            cache.set(int(guild.id), {key: value.value for key, value in found.items()})
        return found

    async def post(
        self,
        guild: discord.Guild,
        spec: ShowcaseSpec,
        *,
        channel_id: int | None = None,
        actor_id: int,
    ) -> ShowcaseResult:
        if channel_id is not None:
            target_id = int(channel_id)
        else:
            target_id = (await self._bound_ids(int(guild.id))).get(spec.channel_key)
        if target_id is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.OFF, error=copy.SHOWCASE_NEED_CHANNEL)
        channel = _channel(guild, int(target_id))
        if channel is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.OFF, error=copy.ADMIN_BAD_CHANNEL)
        await self._retire(guild, spec)
        poster = self._posters[spec.id]
        result = await poster(guild, spec, channel, int(actor_id))
        self._drop_cache(int(guild.id))
        if result.ok:
            return ShowcaseResult(
                ok=True,
                state=await self.status(guild, spec),
                channel_id=int(getattr(channel, "id", 0) or 0),
                message_id=result.message_id,
            )
        return result

    async def refresh(self, guild: discord.Guild, spec: ShowcaseSpec, *, actor_id: int) -> ShowcaseResult:
        bound = await self._bound_ids(int(guild.id))
        channel_id = bound.get(spec.channel_key)
        if channel_id is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.OFF, error=copy.SHOWCASE_NEED_CHANNEL)
        async with session_scope(self._bot.sessions) as session:
            live = await find_live_kind(session, guild_id=int(guild.id), kind=spec.panel_kind)
            live_id = None if live is None else int(live.id)
        if live_id is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SHOWCASE_NEED_LIVE)
        channel = _channel(guild, int(channel_id))
        if channel is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.STALE, error=copy.ADMIN_BAD_CHANNEL)
        poster = self._posters[spec.id]
        result = await poster(guild, spec, channel, int(actor_id))
        self._drop_cache(int(guild.id))
        if result.ok:
            return ShowcaseResult(
                ok=True,
                state=await self.status(guild, spec),
                channel_id=int(channel_id),
                message_id=result.message_id,
            )
        return result

    async def post_missing(self, guild: discord.Guild, *, actor_id: int) -> tuple[ShowcaseResult, ...]:
        self._drop_cache(int(guild.id))
        states = await self.statuses(guild)
        posted: list[ShowcaseResult] = []
        for spec in SHOWCASES:
            if states.get(spec.id) not in {ShowcaseState.MISSING, ShowcaseState.STALE}:
                continue
            posted.append(await self.post(guild, spec, actor_id=actor_id))
        return tuple(posted)

    async def set_channel(self, guild: discord.Guild, spec: ShowcaseSpec, channel_id: int) -> int | None:
        gid = int(guild.id)
        before: int | None = None
        if spec.id == "verify":
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(VerificationSettings, gid)
                if row is None:
                    row = VerificationSettings(guild_id=gid)
                    session.add(row)
                before = None if row.channel_id is None else int(row.channel_id)
                row.channel_id = int(channel_id)
            bind = getattr(getattr(self._bot, "verification", None), "bind", None)
            if callable(bind):
                await bind(gid)
        elif spec.id == "self_roles":
            async with session_scope(self._bot.sessions) as session:
                menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == gid))).all()
                for menu in menus:
                    if before is None and menu.channel_id:
                        before = int(menu.channel_id)
                    menu.channel_id = int(channel_id)
        elif spec.id == "tickets":
            async with session_scope(self._bot.sessions) as session:
                row = await session.scalar(
                    select(TicketPanel).where(TicketPanel.guild_id == gid).order_by(TicketPanel.id)
                )
                if row is None:
                    return None
                before = int(row.channel_id)
                row.channel_id = int(channel_id)
        elif spec.id == "lfg":
            setter = getattr(getattr(self._bot, "lfg", None), "set_channel", None)
            if callable(setter):
                before = await setter(gid, int(channel_id))
        elif spec.id == "stats":
            setter = getattr(getattr(self._bot, "stats_board", None), "set_channel", None)
            if callable(setter):
                before = await setter(gid, int(channel_id))
        elif spec.id == "automod":
            async with session_scope(self._bot.sessions) as session:
                row = await get_or_create_config(session, gid)
                before = None if row.automod_channel_id is None else int(row.automod_channel_id)
                row.automod_channel_id = int(channel_id)
            refresh = getattr(self._bot, "refresh_config", None)
            if callable(refresh):
                await refresh(gid)
        self._drop_cache(gid)
        return before

    async def _status_one(
        self,
        guild: discord.Guild,
        spec: ShowcaseSpec,
        bound: Mapping[str, int | None],
        live: _LiveRow | None,
    ) -> ShowcaseState:
        channel_id = bound.get(spec.channel_key)
        configured = channel_id is not None
        row_live = live is not None and live.state == PANEL_LIVE
        message_alive: bool | None = None
        channel_matches = False
        if live is not None:
            channel_matches = channel_id is not None and int(live.channel_id) == int(channel_id)
            message_alive = await self._message_alive(guild, int(live.channel_id), int(live.message_id))
            if message_alive is False:
                await self._mark_row_stale(int(live.id))
                self._drop_cache(int(guild.id))
        return state_of(
            configured=configured,
            row_live=row_live,
            message_alive=message_alive,
            channel_matches=channel_matches,
        )

    async def _message_alive(self, guild: discord.Guild, channel_id: int, message_id: int) -> bool | None:
        if message_id <= 0:
            return False
        channel = _channel(guild, channel_id)
        if channel is None:
            return False
        fetch = getattr(channel, "fetch_message", None)
        if not callable(fetch):
            return None
        try:
            await self._discord(fetch(int(message_id)))
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return None
        return True

    async def _mark_row_stale(self, panel_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            await mark_stale(session, [int(panel_id)])

    async def _retire(self, guild: discord.Guild, spec: ShowcaseSpec) -> None:
        metas: list[tuple[int, int, int]] = []
        async with session_scope(self._bot.sessions) as session:
            rows = list(
                (
                    await session.scalars(
                        select(Panel).where(
                            Panel.guild_id == int(guild.id),
                            Panel.kind == spec.panel_kind,
                            Panel.state == PANEL_LIVE,
                        )
                    )
                ).all()
            )
            metas = [(int(row.channel_id), int(row.message_id), int(row.id)) for row in rows]
            if rows:
                await mark_stale(session, [item[2] for item in metas])
        for channel_id, message_id, _panel_id in metas:
            await self._delete_message(guild, channel_id, message_id)

    async def _delete_message(self, guild: discord.Guild, channel_id: int, message_id: int) -> None:
        if message_id <= 0:
            return
        channel = _channel(guild, channel_id)
        fetch = getattr(channel, "fetch_message", None) if channel is not None else None
        if not callable(fetch):
            return
        try:
            message = await self._discord(fetch(int(message_id)))
        except (discord.NotFound, discord.HTTPException):
            return
        delete = getattr(message, "delete", None)
        if not callable(delete):
            return
        try:
            await self._discord(delete())
        except (discord.NotFound, discord.HTTPException) as exc:
            log.info("showcase_delete_failed", guild_id=int(guild.id), error=str(exc))

    async def _post_verify(
        self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int
    ) -> ShowcaseResult:
        del spec, channel
        poster = getattr(getattr(self._bot, "verification", None), "post_cards", None)
        if not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_VERIFY_CARDS)
        try:
            ok = bool(await poster(guild, actor_id=actor_id))
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_VERIFY, exc))
        if not ok:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_VERIFY_CARDS)
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def _post_self_roles(
        self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int
    ) -> ShowcaseResult:
        del spec
        service = getattr(self._bot, "self_roles", None)
        ensure = getattr(service, "ensure_defaults", None)
        if callable(ensure):
            try:
                await ensure(int(guild.id))
            except Exception as exc:
                note = copy.seed_note(copy.SEED_SELFROLES, exc)
                return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=note)
        menus_fn = getattr(service, "menus", None)
        poster = getattr(service, "post_menu", None)
        if not callable(menus_fn) or not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_SELFROLES)
        try:
            menus = await menus_fn(int(guild.id))
            for menu in menus:
                error = await poster(guild, int(menu.id), int(channel.id), actor_id=actor_id)
                if error:
                    return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=str(error))
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_SELFROLES, exc))
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def _post_tickets(
        self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int
    ) -> ShowcaseResult:
        del spec
        bound = await self._bound_ids(int(guild.id))
        archive = _channel(guild, bound.get("ticket_archive"))
        if archive is None:
            return ShowcaseResult(ok=False, state=ShowcaseState.OFF, error=copy.SEED_TICKET_PANEL)
        category = _channel(guild, bound.get("cat_tickets"))
        poster = getattr(getattr(self._bot, "tickets", None), "post_panel", None)
        if not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_TICKET_PANEL)
        try:
            ok = bool(
                await poster(
                    guild,
                    channel=channel,
                    archive=archive,
                    actor_id=actor_id,
                    category=category,
                    log_channel_id=bound.get("ticket_log"),
                )
            )
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_TICKETS, exc))
        if not ok:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_TICKET_PANEL)
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def _post_lfg(self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int) -> ShowcaseResult:
        del spec
        poster = getattr(getattr(self._bot, "lfg", None), "post_starter", None)
        if not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_LFG_CARD)
        try:
            ok = bool(await poster(guild, channel, actor_id=actor_id))
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_LFG, exc))
        if not ok:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_LFG_CARD)
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def _post_stats(
        self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int
    ) -> ShowcaseResult:
        del spec
        poster = getattr(getattr(self._bot, "stats_board", None), "post", None)
        if not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_STATS_CARD)
        try:
            ok = bool(await poster(guild, channel, actor_id=actor_id))
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_STATS, exc))
        if not ok:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_STATS_CARD)
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def _post_automod(
        self, guild: discord.Guild, spec: ShowcaseSpec, channel: Any, actor_id: int
    ) -> ShowcaseResult:
        del spec
        poster = getattr(getattr(self._bot, "automod", None), "post_card", None)
        if not callable(poster):
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_AUTOMOD_CARD)
        try:
            ok = bool(await poster(guild, channel, actor_id=actor_id))
        except Exception as exc:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.seed_note(copy.SEED_AUTOMOD, exc))
        if not ok:
            return ShowcaseResult(ok=False, state=ShowcaseState.MISSING, error=copy.SEED_AUTOMOD_CARD)
        return ShowcaseResult(ok=True, state=ShowcaseState.LIVE)

    async def bound_ids(self, guild_id: int) -> dict[str, int | None]:
        return await self._bound_ids(int(guild_id))

    async def _bound_ids(self, guild_id: int) -> dict[str, int | None]:
        ids: dict[str, int | None] = {}
        async with session_scope(self._bot.sessions) as session:
            verify = await session.get(VerificationSettings, guild_id)
            if verify is not None and verify.channel_id:
                ids["verify"] = int(verify.channel_id)
            menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == guild_id))).all()
            for menu in menus:
                if menu.channel_id:
                    ids["roles"] = int(menu.channel_id)
                    break
            lfg = await session.get(LfgSettings, guild_id)
            if lfg is not None and lfg.channel_id:
                ids["lfg"] = int(lfg.channel_id)
            stats = await session.get(StatsBoard, guild_id)
            if stats is not None and stats.channel_id:
                ids["stats"] = int(stats.channel_id)
            config = await session.get(GuildConfig, guild_id)
            if config is not None and config.automod_channel_id:
                ids["automod"] = int(config.automod_channel_id)
            panel = await session.scalar(
                select(TicketPanel).where(TicketPanel.guild_id == guild_id).order_by(TicketPanel.id)
            )
            if panel is not None:
                ids["tickets"] = int(panel.channel_id)
                settings = parse_ticket_settings(panel.settings)
                ids["ticket_archive"] = settings.archive_channel_id
                ids["ticket_log"] = settings.log_channel_id
                ids["cat_tickets"] = settings.discord_category_id
        return ids

    async def _live_rows(self, guild_id: int) -> dict[str, _LiveRow]:
        kinds = tuple(spec.panel_kind for spec in SHOWCASES)
        async with session_scope(self._bot.sessions) as session:
            rows = list(
                (
                    await session.scalars(
                        select(Panel)
                        .where(
                            Panel.guild_id == guild_id,
                            Panel.kind.in_(kinds),
                            Panel.state == PANEL_LIVE,
                        )
                        .order_by(Panel.created_at.desc())
                    )
                ).all()
            )
            found: dict[str, _LiveRow] = {}
            for row in rows:
                found.setdefault(
                    str(row.kind),
                    _LiveRow(
                        id=int(row.id),
                        kind=str(row.kind),
                        channel_id=int(row.channel_id),
                        message_id=int(row.message_id),
                        state=str(row.state),
                    ),
                )
        return found

    def _drop_cache(self, guild_id: int) -> None:
        cache = getattr(getattr(self._bot, "caches", None), "showcase", None)
        if cache is not None:
            cache.invalidate(int(guild_id))

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _channel(guild: object, channel_id: int | None) -> Any:
    if channel_id is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))
