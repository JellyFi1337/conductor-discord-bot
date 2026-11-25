"""Розыгрыши: старт. Отмена и перевыбор — кнопки на панели."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import GIVEAWAY_CREATE
from conductor.domain.giveaway import GiveawayRules, dump_rules, parse_rules
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.giveaway import GiveawayStartModal
from conductor.presentation.layout import denied, respond

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Giveaways(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="giveaway", description=copy.CMD_GIVEAWAY)
    @app_commands.guild_only()
    @app_commands.describe(
        prize_role=copy.CMD_GIVEAWAY_PRIZE_ROLE,
        require_role=copy.CMD_GIVEAWAY_REQUIRE_ROLE,
        consolation_role=copy.CMD_GIVEAWAY_EXTRA_ROLE,
        weight=copy.CMD_GIVEAWAY_WEIGHT,
    )
    @keyed(GIVEAWAY_CREATE)
    async def giveaway(
        self,
        interaction: discord.Interaction,
        prize_role: discord.Role | None = None,
        require_role: discord.Role | None = None,
        consolation_role: discord.Role | None = None,
        weight: bool = False,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await respond(interaction, denied(copy.GIVEAWAY_NO_CHANNEL))
            return
        role_id = None if prize_role is None else prize_role.id
        extra_id = None if consolation_role is None else consolation_role.id
        if not await self.bot.giveaways.prize_role_ok(guild.id, role_id):
            await respond(interaction, denied(copy.GIVEAWAY_BAD_ROLE))
            return
        if not await self.bot.giveaways.prize_role_ok(guild.id, extra_id):
            await respond(interaction, denied(copy.GIVEAWAY_BAD_ROLE))
            return
        messages, voice, cap = await self.bot.giveaways.weight_formula(guild.id)
        rules = parse_rules(
            dump_rules(
                GiveawayRules(
                    require_role_id=None if require_role is None else require_role.id,
                    weight=weight,
                    messages_per_ticket=messages,
                    voice_minutes_per_ticket=voice,
                    max_tickets=cap,
                    consolation_role_id=extra_id,
                )
            )
        )
        await interaction.response.send_modal(GiveawayStartModal(prize_role_id=role_id, rules=rules))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Giveaways(bot))
