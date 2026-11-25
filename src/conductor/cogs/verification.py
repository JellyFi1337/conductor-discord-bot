"""Слушатель канала верификации: кнопка регистрируется импортом карточки."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

import conductor.presentation.verification as _verification_ui  # noqa: F401

if TYPE_CHECKING:
    from conductor.bot import Conductor

__all__ = ["Verification"]


class Verification(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.bot.verification.inspect(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.bot.verification.note_join(member.guild, now=member.joined_at)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Verification(bot))
