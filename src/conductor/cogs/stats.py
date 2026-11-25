"""Статистика сервера и досье: плоская /stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import STATS_READ
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import error_code
from conductor.presentation.motion import InteractionTarget, Progress
from conductor.presentation.stats import TAB_NOW
from conductor.services.stats_cards import guild_view, person_view

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Stats(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="stats", description=copy.CMD_STATS)
    @app_commands.guild_only()
    @app_commands.describe(user=copy.CMD_STATS_USER)
    @keyed(STATS_READ)
    async def stats(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        progress = Progress(
            InteractionTarget(interaction),
            title=copy.STATS_WAIT,
            body=copy.STATS_WAIT_BODY,
            registry=self.bot.motion,
            code=error_code(interaction),
        )
        async with progress:
            await progress.start()
            if user is None:
                view = await guild_view(self.bot, interaction, guild, TAB_NOW)
            else:
                view = await person_view(self.bot, guild, user)
            await progress.finish(view)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Stats(bot))
