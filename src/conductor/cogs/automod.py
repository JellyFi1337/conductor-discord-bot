"""Слушатель автомодерации: факты в очередь, без Discord HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

import conductor.presentation.automod as _automod_ui  # noqa: F401

if TYPE_CHECKING:
    from conductor.bot import Conductor

__all__ = ["Automod"]


class Automod(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.bot.automod.inspect(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        await self.bot.automod.inspect_edit(payload)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Automod(bot))
