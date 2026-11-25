"""Канал верификации-ловушка: кнопка, бан, воронка, всплеск входов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
import structlog
from sqlalchemy import func, select

from conductor.db.models import MemberRecord, ModCase, RoleChange, SelfRoleMenu, TicketPanel, VerificationSettings
from conductor.db.session import session_scope
from conductor.domain.access import Rank, resolve_rank
from conductor.domain.moderation import CaseKind
from conductor.domain.staff_feed import ACTION_JOIN_SPIKE
from conductor.domain.verification import (
    JOIN_SPIKE_WINDOW,
    TRAP_REASON,
    FunnelCounts,
    TrapVerdict,
    everyone_can_send,
    join_spike_due,
    trap_verdict,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import denied, edit_card, event, panel, send_card
from conductor.presentation.tokens import Tone
from conductor.presentation.verification import PANEL_KIND, onboarding_card, rules_card, warning_card
from conductor.services.access import cached_snapshot, log_limit_bypass
from conductor.services.panels import find_usable_kind, mark_live, mark_stale, record_panel
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.staff_feed import emit_staff

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.verification")

__all__ = ["VerificationService"]


class VerificationService:
    def __init__(self, bot: Conductor) -> None:
        self._bot = bot
        # 0 — «канал не настроен», чтобы не ходить в БД на каждое сообщение гильдии.
        self._channels: dict[int, int] = {}
        self._join_spike_at: dict[int, datetime] = {}
        self._role_failures: set[int] = set()

    def channel_id(self, guild_id: int) -> int | None:
        return self._channels.get(int(guild_id)) or None

    async def bind(self, guild_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VerificationSettings, int(guild_id))
            role_id = None if row is None else row.verified_role_id
            channel_id = None if row is None else row.channel_id
        bind = getattr(self._bot.roles, "bind", None)
        if callable(bind):
            bind(int(guild_id), RoleSubsystem.VERIFICATION, () if role_id is None else (int(role_id),))
        self._channels[int(guild_id)] = int(channel_id or 0)
        self._role_failures.discard(int(guild_id))

    async def guard_panel(self, guild_id: int, ref: str) -> GuardResource | None:
        """Стартовая карточка: ref — id гильдии. Нажатие само доказывает, что сообщение живо."""
        if str(int(guild_id)) != str(ref):
            return GuardResource(ref=str(ref), guild_id=int(guild_id), closed=True)
        async with session_scope(self._bot.sessions) as session:
            row = await find_usable_kind(session, guild_id=int(guild_id), kind=PANEL_KIND)
            if row is not None:
                await mark_live(session, [int(row.id)])
        return GuardResource(ref=str(ref), guild_id=int(guild_id))

    async def post_cards(self, guild: object, *, actor_id: int) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VerificationSettings, guild_id)
            if row is None or not row.channel_id or not row.verified_role_id:
                return False
            channel_id = int(row.channel_id)
            warning_id = None if row.warning_message_id is None else int(row.warning_message_id)
        channel = self._channel(guild, channel_id)
        send = getattr(channel, "send", None)
        if not callable(send):
            return False
        view = rules_card(ref=guild_id)
        existing = None
        async with session_scope(self._bot.sessions) as session:
            existing = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
        if existing is not None and int(existing.channel_id) == channel_id and int(existing.message_id) > 0:
            edited = await self._edit_message(guild, channel_id, int(existing.message_id), view)
            panel_id = int(existing.id)
            if edited:
                async with session_scope(self._bot.sessions) as session:
                    await mark_live(session, [panel_id])
            else:
                existing = None
        if existing is None or int(existing.channel_id) != channel_id:
            if existing is not None:
                async with session_scope(self._bot.sessions) as session:
                    await mark_stale(session, [int(existing.id)])
            try:
                message = await self._discord(send_card(channel, view))
            except discord.HTTPException as exc:
                log.warning("verification_card_send_failed", guild_id=guild_id, error=str(exc))
                return False
            async with session_scope(self._bot.sessions) as session:
                stored = await record_panel(
                    session,
                    kind=PANEL_KIND,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=int(message.id),
                    ref=str(guild_id),
                    created_by=int(actor_id),
                )
                panel_id = int(stored.id)
                settings = await session.get(VerificationSettings, guild_id)
                if settings is not None:
                    settings.panel_id = panel_id
        warn = warning_card()
        warn_ok = False
        if warning_id:
            warn_ok = await self._edit_message(guild, channel_id, warning_id, warn)
        if not warn_ok:
            try:
                pinned = await self._discord(send_card(channel, warn))
            except discord.HTTPException as exc:
                log.warning("verification_warn_send_failed", guild_id=guild_id, error=str(exc))
                return False
            pin = getattr(pinned, "pin", None)
            if callable(pin):
                try:
                    await self._discord(pin())
                except discord.HTTPException as exc:
                    log.warning("verification_warn_pin_failed", guild_id=guild_id, error=str(exc))
            async with session_scope(self._bot.sessions) as session:
                settings = await session.get(VerificationSettings, guild_id)
                if settings is not None:
                    settings.warning_message_id = int(pinned.id)
        await self.bind(guild_id)
        return True

    async def grant(self, guild: object, member: object) -> Any:
        guild_id = int(getattr(guild, "id", 0) or 0)
        user_id = int(getattr(member, "id", 0) or 0)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(VerificationSettings, guild_id)
            role_id = None if row is None else row.verified_role_id
        if not role_id:
            return denied(copy.VERIFICATION_NEED_ROLE)
        have = {int(role.id) for role in getattr(member, "roles", ()) or ()}
        already = int(role_id) in have
        if not already:
            apply = getattr(self._bot.roles, "apply", None)
            if callable(apply):
                result = await apply(
                    RoleClaim(
                        guild_id=guild_id,
                        user_id=user_id,
                        subsystem=RoleSubsystem.VERIFICATION,
                        add=(int(role_id),),
                        reason=copy.REASON_VERIFY,
                        urgent=True,
                        actor_id=user_id,
                    )
                )
                granted = bool(getattr(result, "ok", True)) and int(role_id) not in tuple(
                    getattr(result, "dropped", ()) or ()
                )
                if not granted:
                    self._report_role_failure(guild_id, role_id, getattr(result, "error", None))
                    return denied(copy.VERIFICATION_ROLE_FAILED)
        return onboarding_card(copy.onboarding_lines(already=already, **await self._onboarding_channels(guild_id)))

    def _report_role_failure(self, guild_id: int, role_id: int, error: object) -> None:
        """Роль выше бота или занята другой подсистемой — это ошибка настройки, не участника.

        Одна карточка в лог администрации на гильдию, пока роль не сменят: иначе каждый
        новичок даёт по строке.
        """
        log.warning("verification_role_failed", guild_id=guild_id, role_id=int(role_id), error=str(error or ""))
        if guild_id in self._role_failures:
            return
        self._role_failures.add(guild_id)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        submit = getattr(getattr(self._bot, "log_pump", None), "submit", None)
        if channel_id is None or not callable(submit):
            return
        submit(
            int(channel_id),
            view=event(
                copy.VERIFICATION_ROLE_FAILED_LOG,
                str(error or copy.role_missing(role_id)),
                tone=Tone.DANGER,
                source=copy.ADMIN_LOG_SOURCE,
            ),
        )

    async def inspect(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if int(guild.id) not in self._channels:
            await self.bind(guild.id)
        channel_id = self.channel_id(guild.id)
        if channel_id is None or int(message.channel.id) != int(channel_id):
            return False
        if not getattr(self._bot, "serves", lambda _gid: True)(guild.id):
            return False
        # Системные сообщения («X присоединился», буст) приходят от имени участника — это не его текст.
        kind = getattr(message, "type", discord.MessageType.default)
        if kind not in {discord.MessageType.default, discord.MessageType.reply}:
            return True
        author = message.author
        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(int(role.id) for role in getattr(author, "roles", ()) or ())
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        verdict = trap_verdict(
            is_bot=bool(getattr(author, "bot", False)),
            is_webhook=bool(getattr(message, "webhook_id", None)),
            rank=rank,
            is_administrator=bool(getattr(getattr(author, "guild_permissions", None), "administrator", False)),
            is_guild_owner=int(getattr(guild, "owner_id", 0) or 0) == int(getattr(author, "id", 0) or 0),
        )
        if verdict is TrapVerdict.SKIP:
            return True
        if verdict is TrapVerdict.BYPASS:
            log_limit_bypass(guild.id, int(author.id), copy.VERIFICATION_BYPASS, bot=self._bot)
            return True
        await self._trap(guild, author, message)
        return True

    async def funnel(self, guild_id: int, days: int, *, now: datetime | None = None) -> FunnelCounts:
        moment = now or datetime.now(UTC)
        since = moment - timedelta(days=int(days))
        cutoff = timedelta(hours=24)
        async with session_scope(self._bot.sessions) as session:
            joined = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemberRecord)
                    .where(
                        MemberRecord.guild_id == int(guild_id),
                        MemberRecord.is_bot.is_(False),
                        MemberRecord.first_joined_at >= since,
                    )
                )
                or 0
            )
            verified = int(
                await session.scalar(
                    select(func.count(func.distinct(RoleChange.user_id))).where(
                        RoleChange.guild_id == int(guild_id),
                        RoleChange.subsystem == RoleSubsystem.VERIFICATION.value,
                        RoleChange.ok.is_(True),
                        RoleChange.created_at >= since,
                        RoleChange.add_role_ids != "[]",
                    )
                )
                or 0
            )
            trapped = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModCase)
                    .where(
                        ModCase.guild_id == int(guild_id),
                        ModCase.kind == CaseKind.BAN.value,
                        ModCase.reason == TRAP_REASON,
                        ModCase.created_at >= since,
                    )
                )
                or 0
            )
            leavers = (
                await session.execute(
                    select(MemberRecord.first_joined_at, MemberRecord.left_at).where(
                        MemberRecord.guild_id == int(guild_id),
                        MemberRecord.is_bot.is_(False),
                        MemberRecord.first_joined_at >= since,
                        MemberRecord.left_at.is_not(None),
                    )
                )
            ).all()
        left = 0
        for first, left_at in leavers:
            if first is None or left_at is None:
                continue
            if left_at - first <= cutoff:
                left += 1
        return FunnelCounts(joined=joined, verified=verified, trapped=trapped, left_within_24h=left)

    async def note_join(self, guild: object, *, now: datetime | None = None) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        if not guild_id or not getattr(self._bot, "serves", lambda _gid: True)(guild_id):
            return False
        moment = now or datetime.now(UTC)
        since = moment - JOIN_SPIKE_WINDOW
        async with session_scope(self._bot.sessions) as session:
            joined = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemberRecord)
                    .where(
                        MemberRecord.guild_id == guild_id,
                        MemberRecord.is_bot.is_(False),
                        MemberRecord.last_joined_at >= since,
                    )
                )
                or 0
            )
        rules = await self._alert_rules(guild_id)
        floor = int(getattr(rules, "join_spike_floor", 15) or 15)
        last = self._join_spike_at.get(guild_id)
        if not join_spike_due(joined=joined, floor=floor, last=last, now=moment):
            return False
        self._join_spike_at[guild_id] = moment
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=self._bot_id(),
            actor_rank=Rank.SPECIAL,
            action=ACTION_JOIN_SPIKE,
            details=copy.join_spike_body(joined, floor),
            ping_chief=True,
            now=moment,
        )
        return True

    def trap_writable(self, guild: object) -> bool | None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        channel_id = self.channel_id(guild_id)
        if not channel_id:
            return None
        channel = self._channel(guild, channel_id)
        if channel is None:
            return None
        everyone = getattr(guild, "default_role", None)
        overwrite = None
        probe = getattr(channel, "overwrites_for", None)
        if callable(probe) and everyone is not None:
            raw = probe(everyone)
            overwrite = getattr(raw, "send_messages", None)
        inherit = True
        perms = getattr(channel, "permissions_for", None)
        if callable(perms) and everyone is not None:
            inherit = bool(getattr(perms(everyone), "send_messages", False))
        return everyone_can_send(overwrite=overwrite, inherit=inherit)

    async def _trap(self, guild: discord.Guild, author: object, message: discord.Message) -> None:
        delete = getattr(message, "delete", None)
        if callable(delete):
            try:
                await self._discord(delete())
            except discord.HTTPException as exc:
                log.warning("verification_delete_failed", guild_id=guild.id, error=str(exc))
        appeal = await self._appeal(guild.id)
        await self._dm(author, panel(copy.VERIFICATION_TITLE, copy.verification_ban_dm(appeal), tone=Tone.DANGER))
        apply = getattr(self._bot.moderation, "apply", None)
        actor = getattr(guild, "me", None) or SimpleActor(self._bot_id())
        if callable(apply):
            await apply(
                guild,
                actor,
                author,
                kind=CaseKind.BAN,
                reason=TRAP_REASON,
                system=True,
            )

    async def _onboarding_channels(self, guild_id: int) -> dict[str, str | None]:
        tickets = None
        roles = None
        async with session_scope(self._bot.sessions) as session:
            panel_row = await session.scalar(select(TicketPanel).where(TicketPanel.guild_id == int(guild_id)))
            if panel_row is not None:
                tickets = f"<#{int(panel_row.channel_id)}>"
            channel_id = await session.scalar(
                select(SelfRoleMenu.channel_id)
                .where(SelfRoleMenu.guild_id == int(guild_id), SelfRoleMenu.channel_id.is_not(None))
                .order_by(SelfRoleMenu.id)
            )
            if channel_id is not None:
                roles = f"<#{int(channel_id)}>"
        lfg = None
        mention = getattr(getattr(self._bot, "lfg", None), "channel_mention", None)
        if callable(mention):
            lfg = await mention(int(guild_id))
        return {"roles": roles, "lfg": lfg, "tickets": tickets}

    async def _appeal(self, guild_id: int) -> str:
        reputation = getattr(self._bot, "reputation", None)
        show = getattr(reputation, "show_config", None)
        if callable(show):
            panel_cfg = await show(int(guild_id))
            contact = str(getattr(panel_cfg, "appeal_contact", "") or "").strip()
            if contact:
                return contact
        from conductor.presentation import copy

        return copy.APPEAL_DEFAULT

    async def _alert_rules(self, guild_id: int) -> Any:
        pulse = getattr(self._bot, "pulse", None)
        loader = getattr(pulse, "alert_rules", None)
        if callable(loader):
            return await loader(int(guild_id))
        cached = getattr(pulse, "last_alert_rules", None)
        if callable(cached):
            return cached(int(guild_id))
        from conductor.domain.pulse import PulseAlertRules

        return PulseAlertRules()

    async def _dm(self, user: object, view: Any) -> None:
        send = getattr(user, "send", None)
        if not callable(send):
            return
        try:
            await self._discord(send_card(user, view))
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _edit_message(self, guild: object, channel_id: int, message_id: int, view: Any) -> bool:
        channel = self._channel(guild, int(channel_id))
        if channel is None:
            return False
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

    def _channel(self, guild: object, channel_id: int) -> Any:
        getter = getattr(guild, "get_channel", None)
        if not callable(getter):
            return None
        return getter(int(channel_id))

    def _bot_id(self) -> int:
        user = getattr(self._bot, "user", None)
        return int(getattr(user, "id", 0) or 0)

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


class SimpleActor:
    def __init__(self, user_id: int) -> None:
        self.id = int(user_id)
        self.roles: tuple[object, ...] = ()
        self.bot = True
