"""Команда /bug: модалка отчёта об ошибке."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.presentation import copy
from conductor.presentation.bugs import BugReportModal
from conductor.presentation.layout import denied, respond

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Bugs(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="bug", description=copy.CMD_BUG)
    async def bug(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.BUG_USE_SERVER))
            return
        await interaction.response.send_modal(BugReportModal())


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Bugs(bot))
