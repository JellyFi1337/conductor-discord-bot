"""Голосовые комнаты: панель владельца. Кнопки живут на панели."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import VOICE_STAFF_OVERRIDE
from conductor.domain.rooms import StoredRoom
from conductor.presentation import copy
from conductor.presentation.access import allow_key
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, panel, respond
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Voice(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="voice", description=copy.CMD_VOICE)
    @app_commands.guild_only()
    async def voice(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        room = await self._room_in_context(interaction)
        if room is None:
            await respond(interaction, empty(copy.VOICE_NO_ROOM))
            return
        if not await self._can_panel(interaction, room):
            return
        restored = await self.bot.rooms.restore_panel(guild, room, actor_id=interaction.user.id)
        if not restored:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_PANEL_RESTORED, tone=Tone.OK))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        before_id = None if before.channel is None else before.channel.id
        after_id = None if after.channel is None else after.channel.id
        if before_id == after_id:
            return
        self.bot.spawn(
            self.bot.rooms.handle_move(member, before, after),
            name=f"voice-room-{member.guild.id}-{member.id}",
        )

    async def _can_panel(self, interaction: discord.Interaction, room: StoredRoom) -> bool:
        if interaction.user.id == room.owner_id:
            return True
        return await allow_key(interaction, VOICE_STAFF_OVERRIDE)

    async def _room_in_context(self, interaction: discord.Interaction) -> StoredRoom | None:
        guild = interaction.guild
        assert guild is not None
        voice = getattr(interaction.user, "voice", None)
        channel = None if voice is None else getattr(voice, "channel", None)
        if channel is not None:
            found = await self.bot.rooms.room_by_channel(guild.id, channel.id)
            if found is not None:
                return found
        channel_id = interaction.channel_id
        if channel_id is None:
            return None
        return await self.bot.rooms.room_by_channel(guild.id, channel_id)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Voice(bot))
