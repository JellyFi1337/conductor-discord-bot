"""Модерация: замечание, тайм-аут, мут, тяжёлые санкции, чистка, замок."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import Range
from discord.ext import commands

from conductor.domain.access import (
    MOD_BAN,
    MOD_CHANNEL_LOCK,
    MOD_HISTORY,
    MOD_KICK,
    MOD_MUTE,
    MOD_PURGE,
    MOD_TIMEOUT,
    MOD_UNBAN,
    MOD_UNMUTE,
    MOD_WARN,
    REP_PARDON,
)
from conductor.domain.moderation import HISTORY_PAGE, CaseKind, ModVerdict, is_unlock_duration
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, panel, respond
from conductor.presentation.moderation import CaseReasonModal, deliver_case, history_card, outcome_view, run_purge
from conductor.presentation.reputation import pardon_view
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor


@app_commands.guild_only()
class Moderation(commands.GroupCog, group_name="mod"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="warn", description=copy.CMD_MOD_WARN)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_WARN)
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.WARN, reason=reason)

    @app_commands.command(name="timeout", description=copy.CMD_MOD_TIMEOUT)
    @app_commands.describe(user=copy.CMD_MOD_USER, duration=copy.CMD_MOD_DURATION, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_TIMEOUT)
    async def timeout(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.TIMEOUT, reason=reason, duration=duration)

    @app_commands.command(name="mute", description=copy.CMD_MOD_MUTE)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_MUTE)
    async def mute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.MUTE, reason=reason)

    @app_commands.command(name="unmute", description=copy.CMD_MOD_UNMUTE)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_UNMUTE)
    async def unmute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.UNMUTE, reason=reason)

    @app_commands.command(name="kick", description=copy.CMD_MOD_KICK)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_KICK)
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.KICK, reason=reason)

    @app_commands.command(name="ban", description=copy.CMD_MOD_BAN)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_BAN)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.BAN, reason=reason)

    @app_commands.command(name="unban", description=copy.CMD_MOD_UNBAN)
    @app_commands.describe(user=copy.CMD_MOD_USER, reason=copy.CMD_MOD_REASON)
    @keyed(MOD_UNBAN)
    async def unban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str | None = None,
    ) -> None:
        await self._begin(interaction, user, CaseKind.UNBAN, reason=reason)

    @app_commands.command(name="purge", description=copy.CMD_MOD_PURGE)
    @app_commands.describe(
        reason=copy.CMD_MOD_REASON,
        channel=copy.CMD_MOD_CHANNEL,
        user=copy.CMD_MOD_USER,
        after=copy.CMD_MOD_AFTER,
    )
    @keyed(MOD_PURGE)
    async def purge(
        self,
        interaction: discord.Interaction,
        reason: str,
        channel: discord.TextChannel | None = None,
        user: discord.Member | None = None,
        after: str | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        if not await ack(interaction):
            return
        after_message: discord.Message | None = None
        if after:
            try:
                after_message = await target.fetch_message(int(after))
            except (ValueError, discord.HTTPException):
                await respond(interaction, denied(copy.STALE_BODY))
                return
        await run_purge(interaction, target, reason=reason, user=user, after=after_message)

    @app_commands.command(name="lock", description=copy.CMD_MOD_LOCK)
    @app_commands.describe(
        duration=copy.CMD_MOD_DURATION,
        reason=copy.CMD_MOD_REASON,
        channel=copy.CMD_MOD_CHANNEL,
        slowmode=copy.CMD_MOD_SLOWMODE,
    )
    @keyed(MOD_CHANNEL_LOCK)
    async def lock(
        self,
        interaction: discord.Interaction,
        duration: str,
        reason: str | None = None,
        channel: discord.TextChannel | None = None,
        slowmode: Range[int, 0, 21600] | None = None,
    ) -> None:
        guild = interaction.guild
        target = channel or interaction.channel
        if guild is None or not isinstance(target, discord.TextChannel):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        if not await ack(interaction):
            return
        if is_unlock_duration(duration):
            outcome = await self.bot.moderation.unlock_channel(guild, target)
            if outcome.verdict is ModVerdict.OK:
                await respond(interaction, panel(copy.MOD_LOCK_CLEARED, target.mention, tone=Tone.OK))
                return
            await respond(interaction, outcome_view(outcome))
            return
        outcome = await self.bot.moderation.lock_channel(
            guild,
            interaction.user,  # type: ignore[arg-type]
            target,
            reason=reason or "",
            duration_raw=duration,
            slowmode=slowmode,
        )
        if outcome.verdict is ModVerdict.TOO_LONG:
            await respond(interaction, denied(copy.MOD_LOCK_TOO_LONG))
            return
        await respond(interaction, outcome_view(outcome))

    @app_commands.command(name="pardon", description=copy.CMD_MOD_PARDON)
    @app_commands.describe(user=copy.CMD_MOD_USER)
    @keyed(REP_PARDON)
    async def pardon(self, interaction: discord.Interaction, user: discord.Member | discord.User) -> None:
        assert interaction.guild is not None
        if not await ack(interaction):
            return
        result = await self.bot.reputation.pardon(
            interaction.guild,
            user.id,
            actor_id=interaction.user.id,
        )
        await respond(interaction, pardon_view(result))

    @app_commands.command(name="history", description=copy.CMD_MOD_HISTORY)
    @app_commands.describe(user=copy.CMD_MOD_USER)
    @keyed(MOD_HISTORY)
    async def history(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        cases = await self.bot.moderation.history(guild.id, user.id)
        if not cases:
            await respond(interaction, empty(copy.MOD_HISTORY_EMPTY))
            return
        lines = [
            copy.mod_history_line(
                item.number,
                item.kind,
                item.reason,
                revoked_by=item.revoked_by if item.revoked_at else None,
            )
            for item in cases
        ]
        pages = [lines[index : index + HISTORY_PAGE] for index in range(0, len(lines), HISTORY_PAGE)]
        await respond(interaction, history_card(copy.MOD_HISTORY_TITLE.format(user=user.mention), pages, 1))

    async def _begin(
        self,
        interaction: discord.Interaction,
        user: discord.abc.User,
        kind: CaseKind,
        *,
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        if reason is None:
            await interaction.response.send_modal(CaseReasonModal(kind, user, duration=duration))
            return
        await deliver_case(interaction, user, kind=kind, reason=reason, duration=duration)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Moderation(bot))
