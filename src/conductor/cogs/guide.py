"""Справка: команды по рангу и памятка участника."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

from conductor.domain.access import HELP_READ, Rank, resolve_rank
from conductor.domain.guide import (
    TAB_MEMO,
    TAB_MINE,
    TAB_START,
    page_size_for,
    paged_entries,
    parse_help_rank,
)
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.guide import help_card, memo_body, mine_body
from conductor.presentation.layout import denied, respond, update_message
from conductor.services.guide import for_tab, load_snapshot

if TYPE_CHECKING:
    from conductor.bot import Conductor
    from conductor.domain.access import AccessSnapshot

_RANK_CHOICES = [
    app_commands.Choice(name=copy.RANK_WATCHER, value=Rank.WATCHER.key),
    app_commands.Choice(name=copy.RANK_SENIOR, value=Rank.SENIOR.key),
    app_commands.Choice(name=copy.RANK_CHIEF, value=Rank.CHIEF.key),
    app_commands.Choice(name=copy.RANK_SPECIAL, value=Rank.SPECIAL.key),
]


def _role_ids(user: object) -> tuple[int, ...]:
    return tuple(role.id for role in getattr(user, "roles", ()))


class Guide(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(name="help", description=copy.CMD_HELP)
    @app_commands.guild_only()
    @app_commands.describe(rank=copy.CMD_HELP_RANK)
    @app_commands.choices(rank=_RANK_CHOICES)
    @keyed(HELP_READ)
    async def help(
        self,
        interaction: discord.Interaction,
        rank: Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        snapshot = await load_snapshot(self.bot, guild.id)
        if rank is None:
            target = resolve_rank(_role_ids(interaction.user), snapshot.rank_roles)
            developer = self.bot.is_developer(interaction.user.id)
            roles = _role_ids(interaction.user)
            named = False
        else:
            parsed = parse_help_rank(rank.value)
            if parsed is None:
                await respond(interaction, denied(copy.STALE_BODY))
                return
            target = parsed
            developer = False
            roles = ()
            named = True
        await self._reply(
            interaction,
            snapshot=snapshot,
            rank=target,
            tab=TAB_MINE,
            page=1,
            developer=developer,
            role_ids=roles,
            named_rank=named,
        )

    async def _reply(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: AccessSnapshot,
        rank: Rank,
        tab: str,
        page: int,
        developer: bool,
        role_ids: tuple[int, ...],
        named_rank: bool,
        edit: bool = False,
    ) -> None:
        show_start = rank is Rank.SPECIAL
        if tab == TAB_START and not show_start:
            tab = TAB_MINE
        entries = for_tab(
            snapshot,
            rank,
            tab,
            developer=developer,
            member_role_ids=role_ids,
        )
        chunk, current, pages = paged_entries(entries, page, size=page_size_for(tab))
        if tab == TAB_START:
            body = copy.HELP_START
            current, pages = 1, 1
        elif tab == TAB_MEMO:
            body = memo_body(chunk)
        else:
            body = mine_body(chunk)

        async def on_tab(click: discord.Interaction, next_tab: str) -> None:
            if click.guild is None:
                await respond(click, denied(copy.PANEL_GUILD_ONLY))
                return
            await self._reply(
                click,
                snapshot=snapshot,
                rank=rank,
                tab=next_tab,
                page=1,
                developer=developer,
                role_ids=role_ids,
                named_rank=named_rank,
                edit=True,
            )

        async def on_page(click: discord.Interaction, next_page: int) -> None:
            if click.guild is None:
                await respond(click, denied(copy.PANEL_GUILD_ONLY))
                return
            await self._reply(
                click,
                snapshot=snapshot,
                rank=rank,
                tab=tab,
                page=next_page,
                developer=developer,
                role_ids=role_ids,
                named_rank=named_rank,
                edit=True,
            )

        view = help_card(
            tab=tab,
            rank=rank,
            body=body,
            page=current,
            pages=pages,
            named_rank=named_rank,
            show_start=show_start,
            on_tab=on_tab,
            on_page=on_page,
        )
        if edit:
            await update_message(interaction, view)
            return
        await respond(interaction, view)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Guide(bot))
