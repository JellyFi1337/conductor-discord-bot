"""Личная карточка: /profile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.domain.access import MOD_HISTORY, REP_SHOW_OTHER, Rank, resolve_rank
from conductor.presentation import copy
from conductor.presentation.access import allow_key
from conductor.presentation.interact import ack
from conductor.presentation.layout import edit_response, respond
from conductor.presentation.profile import TAB_ACHIEVEMENTS, TAB_ACTIVITY, TAB_REP, profile_card
from conductor.services.access import cached_snapshot

if TYPE_CHECKING:
    from conductor.bot import Conductor


class Profile(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description=copy.CMD_PROFILE)
    @app_commands.guild_only()
    @app_commands.describe(user=copy.CMD_PROFILE_USER)
    async def profile(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            member = interaction.guild.get_member(target.id)
            target = member or target
        if target.id != interaction.user.id and not await allow_key(interaction, REP_SHOW_OTHER):
            return
        if not await ack(interaction):
            return
        await respond(interaction, await self._card(interaction, target, TAB_REP))

    async def _card(
        self, interaction: discord.Interaction, target: discord.abc.User, tab: str
    ) -> discord.ui.LayoutView:
        guild = interaction.guild
        assert guild is not None
        dossier = await self.bot.reputation.dossier(guild.id, target.id)
        card = await self.bot.ladder.card(guild.id, target.id)
        shown = await self.bot.achievements.profile(guild.id, target.id)
        joined = getattr(target, "joined_at", None)
        tenure = max(0, (datetime.now(UTC) - joined).days) if joined is not None else card.tenure_days
        name = getattr(target, "display_name", None) or str(target)

        async def on_tab(switch: discord.Interaction, nxt: str) -> None:
            if nxt not in {TAB_REP, TAB_ACTIVITY, TAB_ACHIEVEMENTS}:
                nxt = TAB_REP
            await self._switch(switch, target, nxt)

        on_dossier = None
        if await self._may_dossier(interaction):

            async def on_dossier(press: discord.Interaction) -> None:
                member = target
                if press.guild is not None and not isinstance(member, discord.Member):
                    member = press.guild.get_member(target.id) or target
                await self.bot.dossier.show(press, member)

        role_ids = tuple(int(role.id) for role in getattr(target, "roles", ()) or ())
        loader = getattr(getattr(self.bot, "self_roles", None), "member_lines", None)
        role_lines = await loader(guild.id, role_ids) if callable(loader) else ()
        lfg_posts = 0
        counter = getattr(getattr(self.bot, "lfg", None), "author_count", None)
        if callable(counter):
            lfg_posts = await counter(guild.id, target.id, days=30)
        return profile_card(
            name=name,
            tenure_days=tenure,
            card=card,
            dossier=dossier,
            tab=tab,
            on_tab=on_tab,
            staff_quota=await self._staff_quota(guild, target),
            on_dossier=on_dossier,
            role_lines=role_lines,
            lfg_posts=int(lfg_posts),
            achievements=shown,
        )

    async def _switch(
        self, interaction: discord.Interaction, target: discord.abc.User, tab: str
    ) -> None:
        if not await ack(interaction):
            return
        await edit_response(interaction, await self._card(interaction, target, tab))

    async def _may_dossier(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        method = getattr(interaction.client, "decide_access", None)
        if guild is None or not callable(method):
            return False
        user = interaction.user
        decision = await method(
            guild_id=guild.id,
            user_id=user.id,
            member_role_ids=tuple(role.id for role in getattr(user, "roles", ()) or ()),
            is_administrator=bool(getattr(getattr(user, "guild_permissions", None), "administrator", False)),
            key=MOD_HISTORY,
        )
        return bool(decision.allowed or decision.developer)

    async def _staff_quota(self, guild: discord.Guild, target: discord.abc.User) -> str | None:
        snapshot = await cached_snapshot(self.bot, guild.id)
        roles = tuple(role.id for role in getattr(target, "roles", ()) or ())
        rank = resolve_rank(roles, snapshot.rank_roles)
        if rank is Rank.NONE:
            return None
        used = await self.bot.moderation.used_today(guild.id, target.id)
        table = await self.bot.moderation.quota_table(guild.id)
        limits = table.for_rank(rank)
        return copy.quota_today(
            timeouts=used.timeouts,
            timeout_limit=limits.timeouts,
            bans=used.bans,
            ban_limit=limits.bans,
        )


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Profile(bot))
