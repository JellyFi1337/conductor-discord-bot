"""Сборы: старт. Отмена — кнопка на панели."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import EVENT_CREATE
from conductor.domain.gathering import DEFAULT_SEATS, parse_seats
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.gathering import GatheringStartModal
from conductor.presentation.layout import denied, respond

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Events(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="event", description=copy.CMD_EVENT)
    @app_commands.guild_only()
    @app_commands.describe(seats=copy.CMD_EVENT_SEATS)
    @keyed(EVENT_CREATE)
    async def event(self, interaction: discord.Interaction, seats: int | None = None) -> None:
        guild = interaction.guild
        assert guild is not None
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await respond(interaction, denied(copy.EVENT_NO_CHANNEL))
            return
        cap = parse_seats(DEFAULT_SEATS if seats is None else seats)
        if cap is None:
            await respond(interaction, denied(copy.EVENT_BAD_SPEC))
            return
        await interaction.response.send_modal(GatheringStartModal(seats=cap))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Events(bot))
