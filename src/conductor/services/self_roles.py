"""Роли по выбору: меню, выдача через брокер, карточки и теги."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import discord
import structlog
from sqlalchemy import func, select

from conductor.db.models import SelfRoleMenu, SelfRoleOption
from conductor.db.session import session_scope
from conductor.domain.access import STAFF_RANKS, AccessSnapshot, Rank
from conductor.domain.self_roles import (
    DEFAULT_MENUS,
    MENU_OPTION_MAX,
    MenuKind,
    OptionTag,
    card_fingerprint,
    default_label_for,
    desired_diff,
    parse_tag,
    sanitize_description,
    sanitize_emoji,
    sanitize_label,
)
from conductor.domain.self_roles import (
    SelfRoleOption as OptionView,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import denied, edit_card, send_card
from conductor.presentation.self_roles import PANEL_KIND, menu_card, pick_card
from conductor.services.panels import find_usable_panel, mark_live, mark_stale_by_ref, record_panel
from conductor.services.role_broker import RoleBindOverlap, RoleClaim, RoleSubsystem
from conductor.services.runtime import log_tick

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.self_roles")

__all__ = ["SelfRolesService", "channels_opened_by"]


def channels_opened_by(guild: object, role: object) -> tuple[str, ...]:
    if role is None:
        return ()
    found: list[str] = []
    seen: set[int] = set()
    buckets: list[object] = []
    for name in ("text_channels", "forums", "voice_channels", "stage_channels"):
        buckets.extend(getattr(guild, name, ()) or ())
    if not buckets:
        buckets = list(getattr(guild, "channels", ()) or ())
    for channel in buckets:
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id in seen:
            continue
        seen.add(channel_id)
        kind = str(getattr(getattr(channel, "type", None), "name", "") or "")
        if kind == "category":
            continue
        probe = getattr(channel, "overwrites_for", None)
        if not callable(probe):
            continue
        if getattr(probe(role), "view_channel", None) is not True:
            continue
        if kind in {"voice", "stage_voice"}:
            found.append(f"🔊 {getattr(channel, 'name', '?')}")
            continue
        mention = getattr(channel, "mention", None)
        found.append(mention if mention else f"#{getattr(channel, 'name', '?')}")
    return tuple(found)


class SelfRolesService:
    def __init__(self, bot: Conductor) -> None:
        self._bot = bot
        self._hashes: dict[int, str] = {}
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(3600, "selfroles-refresh", self.refresh_cards)

    async def bind(self, guild_id: int) -> None:
        await self._bind(guild_id, strict=False)

    async def _bind(self, guild_id: int, *, strict: bool) -> None:
        async with session_scope(self._bot.sessions) as session:
            ids = (
                await session.scalars(select(SelfRoleOption.role_id).where(SelfRoleOption.guild_id == int(guild_id)))
            ).all()
        binder = getattr(self._bot.roles, "bind", None)
        if callable(binder):
            binder(
                int(guild_id),
                RoleSubsystem.SELFROLES,
                tuple(int(role_id) for role_id in ids),
                strict=strict,
            )

    async def _bind_strict(self, guild_id: int) -> None:
        await self._bind(guild_id, strict=True)

    async def ensure_defaults(self, guild_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            count = int(
                await session.scalar(
                    select(func.count()).select_from(SelfRoleMenu).where(SelfRoleMenu.guild_id == int(guild_id))
                )
                or 0
            )
            if count:
                return
            for kind, title in DEFAULT_MENUS:
                session.add(SelfRoleMenu(guild_id=int(guild_id), kind=kind.value, title=title))

    async def menus(self, guild_id: int) -> tuple[SelfRoleMenu, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(SelfRoleMenu).where(SelfRoleMenu.guild_id == int(guild_id)).order_by(SelfRoleMenu.id)
                )
            ).all()
            return tuple(_copy_menu(row) for row in rows)

    async def options(self, menu_id: int) -> tuple[OptionView, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(SelfRoleOption)
                    .where(SelfRoleOption.menu_id == int(menu_id))
                    .order_by(SelfRoleOption.position, SelfRoleOption.id)
                )
            ).all()
            return tuple(_to_option(row) for row in rows)

    def option_role_error(
        self,
        guild_id: int,
        role_id: int,
        *,
        guild: object | None = None,
        snapshot: AccessSnapshot | None = None,
    ) -> str | None:
        if guild is not None:
            getter = getattr(guild, "get_role", None)
            role = getter(int(role_id)) if callable(getter) else None
            if role is None:
                return copy.ADMIN_BAD_ROLE
            default = getattr(guild, "default_role", None)
            if default is not None:
                default_id = int(getattr(default, "id", 0) or 0)
            else:
                default_id = int(getattr(guild, "id", 0) or 0)
            if getattr(role, "managed", False) or int(role_id) == default_id:
                return copy.ADMIN_BAD_ROLE
        if snapshot is not None:
            for rank, ids in snapshot.rank_roles.items():
                if int(role_id) not in {int(item) for item in ids or ()}:
                    continue
                if rank is Rank.SPECIAL:
                    return copy.SELFROLES_SPECIAL
                if rank in STAFF_RANKS:
                    return copy.SELFROLES_STAFF
        owner = self._bot.roles.owner_of(int(guild_id), int(role_id), except_subsystem=RoleSubsystem.SELFROLES)
        if owner is not None:
            return copy.config_role_taken(copy.role_subsystem_label(owner.value))
        return None

    async def add_option(
        self,
        guild_id: int,
        menu_id: int,
        role_id: int,
        *,
        label: str,
        description: str,
        emoji: str | None,
        tag: str,
        guild: object | None = None,
        snapshot: AccessSnapshot | None = None,
    ) -> str | None:
        rejected = self.option_role_error(guild_id, role_id, guild=guild, snapshot=snapshot)
        if rejected:
            return rejected
        parsed = parse_tag(tag)
        if parsed is None:
            return copy.SELFROLES_BAD_TAG
        cleaned = sanitize_label(label) or default_label_for(parsed)
        if not cleaned:
            return copy.SELFROLES_BAD_LABEL
        async with session_scope(self._bot.sessions) as session:
            menu = await session.get(SelfRoleMenu, int(menu_id))
            if menu is None or int(menu.guild_id) != int(guild_id):
                return copy.SELFROLES_NEED_MENU
            taken = await session.scalar(
                select(SelfRoleOption.id).where(
                    SelfRoleOption.guild_id == int(guild_id),
                    SelfRoleOption.role_id == int(role_id),
                )
            )
            if taken is not None:
                return copy.SELFROLES_TAKEN
            total = int(
                await session.scalar(
                    select(func.count()).select_from(SelfRoleOption).where(SelfRoleOption.menu_id == int(menu_id))
                )
                or 0
            )
            if total >= MENU_OPTION_MAX:
                return copy.SELFROLES_LIMIT
            next_pos = int(
                await session.scalar(
                    select(func.max(SelfRoleOption.position)).where(SelfRoleOption.menu_id == int(menu_id))
                )
                or -1
            )
            session.add(
                SelfRoleOption(
                    menu_id=int(menu_id),
                    guild_id=int(guild_id),
                    role_id=int(role_id),
                    label=cleaned,
                    description=sanitize_description(description),
                    emoji=sanitize_emoji(emoji),
                    tag=parsed.value,
                    position=next_pos + 1,
                )
            )
        try:
            await self._bind_strict(guild_id)
        except RoleBindOverlap as exc:
            await self._drop_role(guild_id, role_id)
            return copy.config_role_taken(copy.role_subsystem_label(exc.owner.value))
        await self._sync_card(guild_id, menu_id)
        return None

    async def remove_option(self, guild_id: int, option_id: int) -> str | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(SelfRoleOption, int(option_id))
            if row is None or int(row.guild_id) != int(guild_id):
                return copy.ADMIN_BAD_ROLE
            menu_id = int(row.menu_id)
            await session.delete(row)
        await self.bind(guild_id)
        await self._sync_card(guild_id, menu_id)
        return None

    async def post_menu(self, guild: object, menu_id: int, channel_id: int, *, actor_id: int) -> str | None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        await self.ensure_defaults(guild_id)
        menu = await self._menu(guild_id, menu_id)
        if menu is None:
            return copy.SELFROLES_NEED_MENU
        options = await self._card_options(guild, menu)
        if not options:
            return copy.SELFROLES_EMPTY
        channel = self._channel(guild, int(channel_id))
        send = getattr(channel, "send", None)
        if not callable(send):
            return copy.ADMIN_BAD_CHANNEL
        view = menu_card(menu_id=int(menu.id), title=menu.title, kind=menu.kind, options=options)
        existing = None
        async with session_scope(self._bot.sessions) as session:
            existing = await find_usable_panel(session, guild_id=guild_id, kind=PANEL_KIND, ref=int(menu.id))
        if existing is not None and int(existing.channel_id) == int(channel_id) and int(existing.message_id) > 0:
            edited = await self._edit_message(guild, int(channel_id), int(existing.message_id), view)
            panel_id = int(existing.id)
            if edited:
                async with session_scope(self._bot.sessions) as session:
                    await mark_live(session, [panel_id])
            else:
                existing = None
        if existing is None or int(existing.channel_id) != int(channel_id):
            if existing is not None:
                async with session_scope(self._bot.sessions) as session:
                    await mark_stale_by_ref(session, guild_id=guild_id, kind=PANEL_KIND, ref=int(menu.id))
            try:
                message = await self._discord(send_card(channel, view))
            except discord.HTTPException as exc:
                log.warning("selfroles_card_send_failed", guild_id=guild_id, error=str(exc))
                return copy.STALE_BODY
            async with session_scope(self._bot.sessions) as session:
                stored = await record_panel(
                    session,
                    kind=PANEL_KIND,
                    guild_id=guild_id,
                    channel_id=int(channel_id),
                    message_id=int(message.id),
                    ref=int(menu.id),
                    created_by=int(actor_id),
                )
                panel_id = int(stored.id)
                row = await session.get(SelfRoleMenu, int(menu.id))
                if row is not None:
                    row.channel_id = int(channel_id)
                    row.panel_id = panel_id
        else:
            async with session_scope(self._bot.sessions) as session:
                row = await session.get(SelfRoleMenu, int(menu.id))
                if row is not None:
                    row.channel_id = int(channel_id)
                    row.panel_id = panel_id
        self._hashes[int(menu.id)] = _digest(menu, options)
        await self.bind(guild_id)
        return None

    async def pick(self, guild: object, member: object, menu_id: int, role_ids: Iterable[int]) -> Any:
        guild_id = int(getattr(guild, "id", 0) or 0)
        menu = await self._menu(guild_id, menu_id)
        if menu is None:
            return denied(copy.STALE_BODY)
        options = await self.options(int(menu.id))
        if not options:
            return denied(copy.SELFROLES_EMPTY)
        owned = tuple(item.role_id for item in options)
        have = tuple(int(role.id) for role in getattr(member, "roles", ()) or ())
        add, remove = desired_diff(have=have, menu=owned, want=role_ids)
        labels = {item.role_id: item.label for item in options}
        if add or remove:
            apply = getattr(self._bot.roles, "apply", None)
            if not callable(apply):
                return denied(copy.STALE_BODY)
            result = await apply(
                RoleClaim(
                    guild_id=guild_id,
                    user_id=int(getattr(member, "id", 0) or 0),
                    subsystem=RoleSubsystem.SELFROLES,
                    add=add,
                    remove=remove,
                    reason=copy.REASON_SELF_ROLES,
                    urgent=True,
                    actor_id=int(getattr(member, "id", 0) or 0),
                )
            )
            if not getattr(result, "ok", False):
                return denied(str(getattr(result, "error", None) or copy.STALE_BODY))
        opened: list[str] = []
        if menu.kind == MenuKind.GAME:
            getter = getattr(guild, "get_role", None)
            for role_id in add:
                role = getter(int(role_id)) if callable(getter) else None
                opened.extend(channels_opened_by(guild, role))
        return pick_card(
            granted=tuple(labels[role_id] for role_id in add if role_id in labels),
            removed=tuple(labels[role_id] for role_id in remove if role_id in labels),
            opened=_unique(opened),
        )

    async def guard_panel(self, guild_id: int, ref: str) -> GuardResource | None:
        """Меню ролей: нужна сама запись меню, не живая строка панели."""
        try:
            menu_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            menu = await session.get(SelfRoleMenu, menu_id)
            if menu is None:
                return None
            if int(menu.guild_id) != int(guild_id):
                return GuardResource(ref=str(ref), guild_id=int(menu.guild_id), closed=True)
            row = await find_usable_panel(session, guild_id=int(guild_id), kind=PANEL_KIND, ref=str(ref))
            if row is not None:
                await mark_live(session, [int(row.id)])
        return GuardResource(ref=str(ref), guild_id=int(guild_id))

    async def role_for_tag(self, guild_id: int, tag: str) -> int | None:
        parsed = parse_tag(tag)
        if parsed is None or parsed is OptionTag.NONE:
            return None
        async with session_scope(self._bot.sessions) as session:
            role_id = await session.scalar(
                select(SelfRoleOption.role_id)
                .where(SelfRoleOption.guild_id == int(guild_id), SelfRoleOption.tag == parsed.value)
                .order_by(SelfRoleOption.id)
            )
        return None if role_id is None else int(role_id)

    async def game_options(self, guild_id: int) -> tuple[OptionView, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(SelfRoleOption)
                    .join(SelfRoleMenu, SelfRoleMenu.id == SelfRoleOption.menu_id)
                    .where(SelfRoleOption.guild_id == int(guild_id), SelfRoleMenu.kind == MenuKind.GAME.value)
                    .order_by(SelfRoleMenu.id, SelfRoleOption.position, SelfRoleOption.id)
                )
            ).all()
            return tuple(_to_option(row) for row in rows)

    async def member_lines(self, guild_id: int, role_ids: Iterable[int]) -> tuple[str, ...]:
        have = {int(role_id) for role_id in role_ids}
        lines: list[str] = []
        for menu in await self.menus(guild_id):
            labels = [item.label for item in await self.options(int(menu.id)) if item.role_id in have]
            if labels:
                lines.append(copy.selfroles_member_line(menu.title, labels))
        return tuple(lines)

    async def posted_channel_mention(self, guild_id: int) -> str | None:
        async with session_scope(self._bot.sessions) as session:
            channel_id = await session.scalar(
                select(SelfRoleMenu.channel_id)
                .where(SelfRoleMenu.guild_id == int(guild_id), SelfRoleMenu.channel_id.is_not(None))
                .order_by(SelfRoleMenu.id)
            )
        return None if channel_id is None else f"<#{int(channel_id)}>"

    async def refresh_cards(self) -> None:
        async with session_scope(self._bot.sessions) as session:
            rows = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.panel_id.is_not(None)))).all()
            snapshots = [(_copy_menu(row), int(row.guild_id)) for row in rows]
        changed = 0
        for menu, guild_id in snapshots:
            if not getattr(self._bot, "serves", lambda _gid: True)(guild_id):
                continue
            guild = self._guild(guild_id)
            if guild is None:
                continue
            if await self._refresh_menu(guild, int(menu.id), force=False):
                changed += 1
        log_tick(log, "selfroles_refresh", changed=changed)

    async def _refresh_menu(self, guild: object, menu_id: int, *, force: bool) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        menu = await self._menu(guild_id, menu_id)
        if menu is None or not menu.channel_id:
            return False
        options = await self._card_options(guild, menu)
        if not options:
            return False
        digest = _digest(menu, options)
        if not force:
            previous = self._hashes.get(int(menu.id))
            if previous is None:
                self._hashes[int(menu.id)] = digest
                return False
            if previous == digest:
                return False
        async with session_scope(self._bot.sessions) as session:
            live = await find_usable_panel(session, guild_id=guild_id, kind=PANEL_KIND, ref=int(menu.id))
        if live is None or int(live.message_id) <= 0:
            return False
        view = menu_card(menu_id=int(menu.id), title=menu.title, kind=menu.kind, options=options)
        edited = await self._edit_message(guild, int(live.channel_id), int(live.message_id), view)
        if edited:
            self._hashes[int(menu.id)] = digest
            async with session_scope(self._bot.sessions) as session:
                await mark_live(session, [int(live.id)])
        return edited

    async def _sync_card(self, guild_id: int, menu_id: int) -> None:
        guild = self._guild(guild_id)
        if guild is None:
            return
        await self._refresh_menu(guild, int(menu_id), force=True)

    async def _menu(self, guild_id: int, menu_id: int) -> SelfRoleMenu | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(SelfRoleMenu, int(menu_id))
            if row is None or int(row.guild_id) != int(guild_id):
                return None
            return _copy_menu(row)

    async def _card_options(self, guild: object, menu: SelfRoleMenu) -> tuple[OptionView, ...]:
        items = await self.options(int(menu.id))
        if menu.kind != MenuKind.GAME.value:
            return items
        getter = getattr(guild, "get_role", None)
        opened = []
        for item in items:
            role = getter(int(item.role_id)) if callable(getter) else None
            opened.append(
                OptionView(
                    option_id=item.option_id,
                    menu_id=item.menu_id,
                    role_id=item.role_id,
                    label=item.label,
                    description=item.description,
                    emoji=item.emoji,
                    tag=item.tag,
                    position=item.position,
                    opened=channels_opened_by(guild, role),
                )
            )
        return tuple(opened)

    async def _drop_role(self, guild_id: int, role_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(SelfRoleOption).where(
                    SelfRoleOption.guild_id == int(guild_id),
                    SelfRoleOption.role_id == int(role_id),
                )
            )
            if row is not None:
                await session.delete(row)
        await self.bind(guild_id)

    async def _edit_message(self, guild: object, channel_id: int, message_id: int, view: Any) -> bool:
        channel = self._channel(guild, int(channel_id))
        fetch = getattr(channel, "fetch_message", None)
        target = None
        if callable(fetch):
            try:
                target = await self._discord(fetch(int(message_id)))
            except discord.HTTPException:
                return False
        edit = getattr(target, "edit", None)
        if not callable(edit):
            return False
        try:
            await self._discord(edit_card(target, view))
        except discord.HTTPException:
            return False
        return True

    def _guild(self, guild_id: int) -> Any:
        getter = getattr(self._bot, "get_guild", None)
        if not callable(getter):
            return None
        return getter(int(guild_id))

    def _channel(self, guild: object, channel_id: int) -> Any:
        getter = getattr(guild, "get_channel", None)
        if not callable(getter):
            return None
        return getter(int(channel_id))

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _copy_menu(row: SelfRoleMenu) -> SelfRoleMenu:
    return SelfRoleMenu(
        id=int(row.id),
        guild_id=int(row.guild_id),
        kind=str(row.kind),
        title=str(row.title),
        channel_id=None if row.channel_id is None else int(row.channel_id),
        panel_id=None if row.panel_id is None else int(row.panel_id),
    )


def _to_option(row: SelfRoleOption, opened: tuple[str, ...] = ()) -> OptionView:
    return OptionView(
        option_id=int(row.id),
        menu_id=int(row.menu_id),
        role_id=int(row.role_id),
        label=str(row.label),
        description=str(row.description or ""),
        emoji=None if not row.emoji else str(row.emoji),
        tag=parse_tag(row.tag) or OptionTag.NONE,
        position=int(row.position or 0),
        opened=opened,
    )


def _digest(menu: SelfRoleMenu, options: Iterable[OptionView]) -> str:
    return card_fingerprint(
        kind=str(menu.kind),
        title=str(menu.title),
        options=tuple((item.role_id, item.label, item.description, item.emoji, item.opened) for item in options),
    )


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    found: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        found.append(item)
    return tuple(found)
