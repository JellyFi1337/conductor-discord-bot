"""Рекрут-ссылки: выдача, отзыв, отчёт."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import RECRUIT_MANAGE, RECRUIT_READ
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, respond
from conductor.presentation.recruit import link_card, listing_card, report_card

if TYPE_CHECKING:
    from conductor.bot import Conductor


@app_commands.guild_only()
class Recruit(commands.GroupCog, group_name="recruit"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="link", description=copy.CMD_RECRUIT_LINK)
    @app_commands.describe(
        user=copy.CMD_RECRUIT_USER,
        channel=copy.CMD_RECRUIT_CHANNEL,
        revoke=copy.CMD_RECRUIT_REVOKE,
    )
    @keyed(RECRUIT_MANAGE)
    async def link(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        channel: discord.TextChannel | None = None,
        revoke: bool = False,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        if revoke:
            revoked, error = await self.bot.recruit.revoke(guild, user.id, actor_id=interaction.user.id)
            if error is not None or revoked is None:
                await respond(interaction, empty(error or copy.RECRUIT_NO_LINK))
                return
            card = await self.bot.recruit.report_one(guild, code=revoked.code)
            if card is None:
                await respond(interaction, empty(copy.RECRUIT_NOT_FOUND))
                return
            await respond(interaction, report_card(card))
            return
        issued, error = await self.bot.recruit.issue(
            guild,
            user.id,
            actor_id=interaction.user.id,
            channel=channel,
        )
        if error is not None or issued is None:
            await respond(interaction, denied(error or copy.RECRUIT_INVITE_FAILED))
            return
        await respond(interaction, link_card(issued))

    @app_commands.command(name="report", description=copy.CMD_RECRUIT_REPORT)
    @app_commands.describe(code=copy.CMD_RECRUIT_CODE, user=copy.CMD_RECRUIT_OWNER)
    @keyed(RECRUIT_READ)
    async def report(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
        user: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        if code or user is not None:
            card = await self.bot.recruit.report_one(
                guild,
                code=code,
                owner_id=None if user is None else user.id,
            )
            if card is None:
                await respond(interaction, empty(copy.RECRUIT_NOT_FOUND))
                return
            await respond(interaction, report_card(card))
            return
        listing = await self.bot.recruit.report_listing(guild, page=1)
        if not listing.rows:
            await respond(interaction, empty(copy.RECRUIT_EMPTY))
            return
        await respond(interaction, listing_card(listing))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Recruit(bot))
