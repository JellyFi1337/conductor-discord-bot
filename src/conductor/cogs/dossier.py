"""Контекстное меню «Досье» на участнике."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import MOD_HISTORY
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, respond

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Dossier(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot
        self.menu = app_commands.ContextMenu(name=copy.CMD_DOSSIER, callback=self.open_member)
        self.menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=discord.AppCommandType.user)

    @keyed(MOD_HISTORY)
    async def open_member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await ack(interaction):
            return
        await self.bot.dossier.show(interaction, member)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Dossier(bot))
