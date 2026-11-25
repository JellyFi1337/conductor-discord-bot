"""Опросы: старт, закрытие, удаление, раскрытие анонимных."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

from conductor.domain.access import POLL_CREATE, POLL_DEANONYMIZE
from conductor.domain.poll import ManageVerdict, PollKind, parse_kind
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, panel, respond
from conductor.presentation.poll import PollStartModal
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor

_KIND_CHOICES = [
    app_commands.Choice(name=copy.POLL_KIND_PUBLIC, value=str(PollKind.PUBLIC)),
    app_commands.Choice(name=copy.POLL_KIND_ANONYMOUS, value=str(PollKind.ANONYMOUS)),
    app_commands.Choice(name=copy.POLL_KIND_HIDDEN, value=str(PollKind.HIDDEN)),
]


@app_commands.guild_only()
class Polls(commands.GroupCog, group_name="poll"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="start", description=copy.CMD_POLL_START)
    @app_commands.describe(kind=copy.CMD_POLL_KIND, allow_change=copy.CMD_POLL_CHANGE)
    @app_commands.choices(kind=_KIND_CHOICES)
    @keyed(POLL_CREATE)
    async def start(
        self,
        interaction: discord.Interaction,
        kind: Choice[str] | None = None,
        allow_change: bool = True,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        flavour = parse_kind(None if kind is None else kind.value) or PollKind.PUBLIC
        await interaction.response.send_modal(PollStartModal(kind=flavour, allow_change=allow_change))

    @app_commands.command(name="deanonymize", description=copy.CMD_POLL_REVEAL)
    @keyed(POLL_DEANONYMIZE)
    async def deanonymize(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        card = await self.bot.polls.latest_anonymous(guild.id, int(interaction.channel_id or 0))
        if card is None:
            await respond(interaction, empty(copy.POLL_NO_ANON))
            return
        result = await self.bot.polls.deanonymize(guild, card.poll_id, actor_id=interaction.user.id)
        if result is ManageVerdict.OK:
            await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_REVEAL_OK, tone=Tone.OK))
            return
        if result is ManageVerdict.OPEN:
            await respond(interaction, denied(copy.POLL_STILL_OPEN))
            return
        if result is ManageVerdict.NOT_ANON:
            await respond(interaction, denied(copy.POLL_NOT_ANON))
            return
        if result is ManageVerdict.ALREADY:
            await respond(interaction, empty(copy.POLL_REVEAL_ALREADY))
            return
        await respond(interaction, empty(copy.STALE_BODY))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Polls(bot))
