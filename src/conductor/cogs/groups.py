"""Приватные группы: создание и «Мои группы». Кнопки живут на шапке и в ЛС."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.groups import CreateVerdict, parse_spec
from conductor.presentation import copy
from conductor.presentation.groups import mine_view
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, panel, respond
from conductor.presentation.motion import Countdown
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Groups(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="group", description=copy.CMD_GROUP)
    @app_commands.guild_only()
    @app_commands.describe(name=copy.CMD_GROUP_NAME, members=copy.CMD_GROUP_MEMBERS, voice=copy.CMD_GROUP_VOICE)
    async def group(
        self,
        interaction: discord.Interaction,
        name: str,
        members: str | None = None,
        voice: bool = False,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        spec = parse_spec(title=name, members=members, owner_id=interaction.user.id, with_voice=voice)
        if spec is None:
            await respond(interaction, denied(copy.GROUP_BAD_SPEC))
            return
        if not await ack(interaction):
            return
        report = await self.bot.groups.create(guild, interaction.user, spec)
        if report.verdict is CreateVerdict.OK:
            body = copy.group_created_body(
                report.title, int(report.channel_id or 0), report.invites_sent, report.invites_closed
            )
            await respond(interaction, panel(copy.GROUP_TITLE, body, tone=Tone.OK))
            return
        if report.verdict is CreateVerdict.LOCKED:
            await respond(interaction, denied(copy.GROUP_LOCKED))
            return
        if report.verdict is CreateVerdict.BAD_SPEC:
            await respond(interaction, denied(copy.GROUP_BAD_SPEC))
            return
        if report.verdict is CreateVerdict.REPUTATION:
            await respond(
                interaction, denied(copy.group_reputation_denied(report.score, report.min_reputation))
            )
            return
        if report.verdict is CreateVerdict.REPUTATION_OFF:
            await respond(interaction, denied(copy.GROUP_REPUTATION_OFF))
            return
        if report.verdict is CreateVerdict.QUOTA_DAILY:
            retry = copy.GROUP_LATER if report.retry_at is None else Countdown().render(report.retry_at)
            await respond(interaction, denied(copy.group_quota_daily(retry)))
            return
        if report.verdict is CreateVerdict.QUOTA_OPEN:
            await respond(interaction, denied(copy.group_quota_open(report.open_limit)))
            return
        await respond(interaction, denied(copy.GROUP_FAILED))

    @app_commands.command(name="groups", description=copy.CMD_GROUPS)
    @app_commands.guild_only()
    async def groups(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        listing = await self.bot.groups.mine(guild, interaction.user)
        await respond(interaction, mine_view(listing))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        await self.bot.groups.touch_activity(message.guild.id, message.channel.id)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Groups(bot))
