from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from conductor.domain.staff_feed import ACTION_EXTERNAL
from conductor.presentation import copy
from conductor.presentation.audit import (
    SKIP_AUDIT_ALWAYS,
    SKIP_IF_USER_LOGS_SAME_CHANNEL,
    audit_card,
    automod_card,
    format_audit_changes,
)
from conductor.services.staff_feed import actor_rank_of, emit_staff

if TYPE_CHECKING:
    from conductor.bot import Conductor

ACTION_TITLES = copy.AUDIT_LABELS


def _title(action: discord.AuditLogAction) -> str:
    return copy.audit_title(action.name)


class AdminLogs(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    def _channel_id(self, guild_id: int) -> int | None:
        config = self.bot.config_by_guild.get(guild_id)
        return None if config is None else config.admin_log_channel_id

    def _send(self, guild_id: int, view: discord.ui.LayoutView) -> None:
        self.bot.log_pump.submit(self._channel_id(guild_id), view=view)

    def _skip_audit(self, entry: discord.AuditLogEntry) -> bool:
        action = entry.action.name
        if action in SKIP_AUDIT_ALWAYS:
            return True
        me = entry.guild.me
        if me is not None and entry.user is not None and entry.user.id == me.id and action == "member_role_update":
            return True
        config = self.bot.config_by_guild.get(entry.guild.id)
        if (
            config is not None
            and config.user_log_channel_id
            and config.user_log_channel_id == config.admin_log_channel_id
            and action in SKIP_IF_USER_LOGS_SAME_CHANNEL
        ):
            return True
        return not format_audit_changes(entry)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        await self.record_external(entry)
        if self._skip_audit(entry):
            return
        self._send(entry.guild.id, audit_card(entry, _title(entry.action)))

    async def record_external(self, entry: discord.AuditLogEntry) -> None:
        if not _is_external_sanction(entry, self.bot):
            return
        user = entry.user
        target = getattr(entry, "target", None)
        target_id = getattr(target, "id", None)
        actor_id = 0 if user is None else int(user.id)
        rank = await actor_rank_of(self.bot, entry.guild, actor_id) if actor_id else 0
        await emit_staff(
            self.bot,
            entry.guild.id,
            actor_id=actor_id,
            actor_rank=rank,
            action=ACTION_EXTERNAL,
            target=None if target_id is None else int(target_id),
            ref=getattr(entry, "id", None),
            details=_external_detail(entry),
            ping_chief=True,
        )

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        self._send(execution.guild_id, automod_card(execution))


def _action_name(entry: object) -> str:
    action = getattr(entry, "action", None)
    return str(getattr(action, "name", action) or "")


def _is_external_sanction(entry: object, bot: object) -> bool:
    user = getattr(entry, "user", None)
    bot_user = getattr(bot, "user", None)
    bot_id = getattr(bot_user, "id", None)
    user_id = getattr(user, "id", None)
    if user_id is None or (bot_id is not None and int(user_id) == int(bot_id)):
        return False
    name = _action_name(entry)
    if name in {"ban", "kick"}:
        return True
    if name != "member_update":
        return False
    after = getattr(entry, "after", None)
    if after is None:
        return False
    until = getattr(after, "timed_out_until", None) or getattr(after, "communication_disabled_until", None)
    return until is not None


def _external_detail(entry: object) -> str:
    name = _action_name(entry)
    if name == "kick":
        return "kick"
    if name == "member_update":
        return "timeout"
    return "ban"


async def setup(bot: Conductor) -> None:
    await bot.add_cog(AdminLogs(bot))
