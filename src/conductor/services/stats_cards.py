"""Сборка карточек /stats: вкладки гильдии и досье."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from conductor.db.session import session_scope
from conductor.domain.access import PULSE_STAFF
from conductor.presentation import copy
from conductor.presentation.interact import ack
from conductor.presentation.layout import edit_response
from conductor.presentation.stats import (
    TAB_CHANNELS,
    TAB_MEMBERS,
    TAB_NOW,
    TAB_OVERVIEW,
    TAB_TIES,
    channels_fields,
    guild_card,
    guild_ties_fields,
    members_fields,
    overview_fields,
    person_card,
    person_fields,
    person_ties_card,
    person_ties_fields,
)
from conductor.services.guild_config import get_or_create_config
from conductor.services.history import load_channel_ranks, load_coverage, load_dossier, load_pairs
from conductor.services.stats import build_overview


async def is_staff(bot: Any, interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        return False
    decision = await bot.decide_access(
        guild_id=guild.id,
        user_id=user.id,
        member_role_ids=tuple(role.id for role in getattr(user, "roles", ())),
        is_administrator=bool(getattr(getattr(user, "guild_permissions", None), "administrator", False)),
        key=PULSE_STAFF,
    )
    return bool(getattr(decision, "allowed", False))


async def guild_view(
    bot: Any,
    interaction: discord.Interaction,
    guild: discord.Guild,
    tab: str,
) -> discord.ui.LayoutView:
    async def on_tab(switch: discord.Interaction, nxt: str) -> None:
        if not await ack(switch):
            return
        await edit_response(switch, await guild_view(bot, switch, guild, nxt))

    snapped = datetime.now(UTC)
    if tab == TAB_NOW:
        snapshot = await bot.pulse.snapshot(guild)
        return guild_card(tab=tab, snapshot=snapshot, staff=await is_staff(bot, interaction), on_tab=on_tab)
    async with session_scope(bot.sessions) as session:
        if tab == TAB_OVERVIEW:
            config = await get_or_create_config(session, guild.id)
            stats = await build_overview(session, guild.id)
            coverage = await load_coverage(session, guild.id)
            pairs = await load_pairs(session, guild.id)
            snapshot = await bot.pulse.snapshot(guild)
            return guild_card(
                tab=tab,
                title=guild.name,
                fields=overview_fields(
                    guild,
                    census_at=config.census_at,
                    coverage=coverage,
                    stats=stats,
                    pairs=pairs,
                    snapshot=snapshot,
                    zone=copy.STATS_ZONE,
                ),
                snapped_at=snapped,
                on_tab=on_tab,
            )
        if tab == TAB_MEMBERS:
            stats = await build_overview(session, guild.id)
            return guild_card(
                tab=tab,
                title=copy.STATS_TAB_MEMBERS,
                fields=members_fields(guild, stats),
                snapped_at=snapped,
                on_tab=on_tab,
            )
        if tab == TAB_CHANNELS:
            ranks = await load_channel_ranks(session, guild.id)
            if not ranks:
                return guild_card(
                    tab=tab,
                    title=copy.STATS_TAB_CHANNELS,
                    body=copy.STATS_CHANNELS_EMPTY,
                    snapped_at=snapped,
                    on_tab=on_tab,
                )
            return guild_card(
                tab=tab,
                title=copy.STATS_TAB_CHANNELS,
                fields=channels_fields(guild, ranks),
                snapped_at=snapped,
                on_tab=on_tab,
            )
        pairs = await load_pairs(session, guild.id)
        body, fields = guild_ties_fields(guild, pairs)
        return guild_card(
            tab=TAB_TIES,
            title=copy.STATS_TIES_TITLE,
            body=body,
            fields=fields,
            snapped_at=snapped,
            on_tab=on_tab,
        )


async def person_ties(bot: Any, guild: discord.Guild, user: discord.abc.User) -> discord.ui.LayoutView:
    snapped = datetime.now(UTC)
    async with session_scope(bot.sessions) as session:
        pairs = await load_pairs(session, guild.id)
    body, fields = person_ties_fields(guild, pairs, user.id)
    return person_ties_card(str(user), body=body, fields=fields, snapped_at=snapped)


async def person_view(bot: Any, guild: discord.Guild, user: discord.abc.User) -> discord.ui.LayoutView:
    snapped = datetime.now(UTC)
    async with session_scope(bot.sessions) as session:
        dossier = await load_dossier(session, guild.id, user.id)

    async def on_ties(switch: discord.Interaction) -> None:
        if not await ack(switch):
            return
        await edit_response(switch, await person_ties(bot, guild, user))

    return person_card(str(user), fields=person_fields(guild, dossier), snapped_at=snapped, on_ties=on_ties)
