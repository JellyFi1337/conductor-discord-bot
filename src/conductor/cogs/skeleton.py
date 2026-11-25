"""Слушает создание и переименование строк каркаса и ставит их в очередь привязки."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import structlog
from discord.ext import commands

from conductor.domain.seed import SkeletonKind

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.skeleton")


def _kind(obj: object) -> SkeletonKind | None:
    if isinstance(obj, discord.Role):
        return SkeletonKind.ROLE
    if isinstance(obj, discord.CategoryChannel):
        return SkeletonKind.CATEGORY
    if isinstance(obj, discord.VoiceChannel):
        return SkeletonKind.VOICE
    if isinstance(obj, discord.TextChannel):
        return SkeletonKind.TEXT
    return None


class Skeleton(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        self.bot.seed.cancel_pending()

    def _offer(self, guild: discord.Guild | None, obj: object) -> None:
        try:
            if guild is None:
                return
            kind = _kind(obj)
            name = str(getattr(obj, "name", "") or "")
            if kind is None or not name:
                return
            self.bot.seed.schedule_ingest(guild, kind, name)
        except Exception:
            log.exception("skeleton_offer_failed")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        self._offer(channel.guild, channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if str(getattr(before, "name", "") or "") == str(getattr(after, "name", "") or ""):
            return
        self._offer(after.guild, after)
        try:
            await self.bot.structure.on_renamed(after, old_name=str(getattr(before, "name", "") or ""))
        except Exception:
            log.exception("skeleton_rename_failed")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            guild = channel.guild
            loss = await self.bot.structure.on_channel_gone(guild, int(channel.id))
            if loss is not None:
                await self.bot.structure.notify(guild, loss)
        except Exception:
            log.exception("skeleton_channel_gone_failed")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        try:
            loss = await self.bot.structure.on_role_gone(role.guild, int(role.id))
            if loss is not None:
                await self.bot.structure.notify(role.guild, loss)
        except Exception:
            log.exception("skeleton_role_gone_failed")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        try:
            await self.bot.structure.sweep_all()
        except Exception:
            log.exception("skeleton_sweep_failed")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        self._offer(role.guild, role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if str(before.name or "") == str(after.name or ""):
            return
        self._offer(after.guild, after)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Skeleton(bot))
