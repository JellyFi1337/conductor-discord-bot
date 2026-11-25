"""Тикеты: слушатели. Кнопки живут на карточке обращения."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    import discord

    from conductor.bot import Conductor


class Tickets(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await self.bot.tickets.relay_dm(message)
            return
        await self.bot.tickets.on_guild_message(message)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Tickets(bot))
