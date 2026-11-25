"""Напоминания: постановка или список с отменой."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import REMIND_USE
from conductor.domain.reminders import CancelVerdict, CreateVerdict
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, respond, update_message
from conductor.presentation.reminders import cancelled_view, created_view, list_view

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Reminders(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="remind", description=copy.CMD_REMIND)
    @app_commands.guild_only()
    @app_commands.describe(when=copy.CMD_REMIND_WHEN, text=copy.CMD_REMIND_TEXT)
    @keyed(REMIND_USE)
    async def remind(
        self,
        interaction: discord.Interaction,
        when: str | None = None,
        text: str | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        if when is None and text is None:
            await self._list(interaction)
            return
        if when is None or text is None:
            await respond(
                interaction,
                denied(copy.REMIND_BAD_WHEN if when is None else copy.REMIND_BAD_TEXT),
            )
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await respond(interaction, denied(copy.REMIND_NO_CHANNEL))
            return
        if not await ack(interaction):
            return
        verdict, card = await self.bot.reminders.create(guild.id, channel_id, interaction.user.id, when=when, text=text)
        if verdict is CreateVerdict.BAD_WHEN:
            await respond(interaction, denied(copy.REMIND_BAD_WHEN))
            return
        if verdict is CreateVerdict.BAD_TEXT:
            await respond(interaction, denied(copy.REMIND_BAD_TEXT))
            return
        if verdict is CreateVerdict.LIMIT:
            await respond(interaction, denied(copy.REMIND_LIMIT))
            return
        assert card is not None
        await respond(interaction, created_view(card))

    async def _list(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        actor_id = interaction.user.id
        cards = await self.bot.reminders.pending(guild.id, actor_id)

        async def on_cancel(click: discord.Interaction, reminder_id: int) -> None:
            if click.user.id != actor_id:
                await respond(click, denied(copy.REMIND_MISSING))
                return
            verdict, card = await self.bot.reminders.cancel(guild.id, actor_id, reminder_id)
            if verdict is CancelVerdict.MISSING:
                await respond(click, denied(copy.REMIND_MISSING))
                return
            if verdict is CancelVerdict.ALREADY:
                await respond(click, denied(copy.REMIND_ALREADY))
                return
            remaining = await self.bot.reminders.pending(guild.id, actor_id)
            if remaining:
                await update_message(click, list_view(remaining, on_cancel=on_cancel))
                return
            assert card is not None
            await update_message(click, cancelled_view(card))

        await respond(interaction, list_view(cards, on_cancel=on_cancel))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Reminders(bot))
