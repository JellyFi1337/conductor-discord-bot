"""Карточки /stats: вкладки сервера и досье. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import discord
from discord import ui

from conductor.domain.history import PairScore, partners_for
from conductor.domain.pulse import PulseSnapshot
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import panel
from conductor.presentation.pulse import now_view
from conductor.presentation.text import FOOTER_STATS, channel_label, stamp, user_plain
from conductor.presentation.tokens import Tone

TAB_NOW = "now"
TAB_OVERVIEW = "overview"
TAB_MEMBERS = "members"
TAB_CHANNELS = "channels"
TAB_TIES = "ties"
TABS: tuple[tuple[str, str], ...] = (
    (TAB_NOW, copy.STATS_TAB_NOW),
    (TAB_OVERVIEW, copy.STATS_TAB_OVERVIEW),
    (TAB_MEMBERS, copy.STATS_TAB_MEMBERS),
    (TAB_CHANNELS, copy.STATS_TAB_CHANNELS),
    (TAB_TIES, copy.STATS_TAB_TIES),
)

TabSwitch = Callable[[discord.Interaction, str], Awaitable[None]]
TiesClick = Callable[[discord.Interaction], Awaitable[None]]


def user_name(guild: discord.Guild, user_id: int) -> str:
    return user_plain(guild.get_member(user_id), user_id)


def channel_name(guild: discord.Guild, channel_id: int) -> str:
    return channel_label(guild.get_channel(channel_id) or guild.get_thread(channel_id), channel_id)


def pair_line(guild: discord.Guild, left_id: int, right_id: int, strength: int, events: int) -> str:
    return f"{user_name(guild, left_id)} ↔ {user_name(guild, right_id)}\nсвязь **{strength}** · {events} касаний"


def partner_line(guild: discord.Guild, other_id: int, pair: PairScore) -> str:
    return (
        f"{user_name(guild, other_id)} — связь **{pair.strength}**\n"
        f"ответы {pair.replies} · упоминания {pair.mentions} · реакции {pair.reactions}"
    )


def snap_footnote(when: datetime) -> str:
    return f"{FOOTER_STATS} · {copy.stats_snap(stamp(when, 'R'))}"


def guild_card(
    *,
    tab: str,
    snapshot: PulseSnapshot | None = None,
    staff: bool = False,
    title: str = "",
    body: str = "",
    fields: Mapping[str, object] | None = None,
    snapped_at: datetime | None = None,
    on_tab: TabSwitch | None = None,
) -> ui.LayoutView:
    rows = (_tab_row(tab, on_tab),) if on_tab is not None else ()
    if tab == TAB_NOW and snapshot is not None:
        return now_view(snapshot, staff=staff, rows=rows)
    footnote = snap_footnote(snapped_at) if snapped_at is not None else FOOTER_STATS
    return panel(title, body, tone=Tone.INFO, fields=fields, footnote=footnote, rows=rows)


def person_card(
    title: str,
    *,
    fields: Mapping[str, object] | None,
    snapped_at: datetime,
    on_ties: TiesClick | None = None,
) -> ui.LayoutView:
    rows: tuple[ui.ActionRow, ...] = ()
    if on_ties is not None:
        button = ui.Button(label=copy.STATS_TIES_BTN, style=discord.ButtonStyle.secondary)
        button.callback = instant(on_ties)
        rows = (ui.ActionRow(button),)
    return panel(
        title,
        copy.STATS_PERSON_BODY,
        tone=Tone.INFO,
        fields=fields,
        footnote=snap_footnote(snapped_at),
        rows=rows,
    )


def person_ties_card(
    title: str,
    *,
    body: str,
    fields: Mapping[str, object] | None,
    snapped_at: datetime,
) -> ui.LayoutView:
    return panel(title, body, tone=Tone.INFO, fields=fields, footnote=snap_footnote(snapped_at))


def overview_fields(
    guild: discord.Guild,
    *,
    census_at: datetime | None,
    coverage: Any,
    stats: Any,
    pairs: Sequence[Any],
    snapshot: PulseSnapshot | None = None,
    zone: str = copy.STATS_ZONE,
) -> dict[str, object]:
    fields: dict[str, object] = {
        copy.STATS_SERVER_CREATED: stamp(guild.created_at, "D"),
        copy.STATS_LAST_CENSUS: stamp(census_at) if census_at else copy.STATS_CENSUS_NEVER,
    }
    if coverage.messages:
        oldest = stamp(discord.utils.snowflake_time(coverage.oldest_id), "D") if coverage.oldest_id else "—"
        newest = stamp(discord.utils.snowflake_time(coverage.newest_id), "D") if coverage.newest_id else "—"
        fields[copy.STATS_HISTORY] = (
            f"**{coverage.messages}** сообщ. · **{coverage.channels}** каналов\n{oldest} — {newest}"
        )
    else:
        fields[copy.STATS_HISTORY] = copy.STATS_HISTORY_EMPTY
    humans = snapshot.inputs.humans if snapshot is not None else stats.humans
    bots = snapshot.inputs.bots if snapshot is not None else stats.bots
    boosters = snapshot.inputs.boosters if snapshot is not None else stats.boosters
    fields[copy.STATS_COMPOSITION] = (
        f"люди **{humans}** · боты **{bots}** · бусты **{boosters}**\n"
        f"ушли (в базе) **{stats.left}** · медиана стажа **{stats.median_tenure_days}** дн."
    )
    fields[copy.STATS_JOINS] = f"7 дней **{stats.joined_7d}** · 30 дней **{stats.joined_30d}**"
    if stats.tenure:
        tenure = "\n".join(f"{label} — **{count}**" for label, count in stats.tenure if count)
        if tenure:
            fields[copy.STATS_TENURE] = tenure
    if stats.join_months:
        fields[copy.STATS_JOIN_WAVES] = "\n".join(
            f"{moment:%Y-%m} — **{count}**" for moment, count in stats.join_months[:8]
        )
    if stats.top_message_days:
        fields[copy.STATS_MSG_PEAKS] = "\n".join(
            f"{row.period_start:%Y-%m-%d} — **{row.count}**" for row in stats.top_message_days[:6]
        )
    if stats.top_voice_days:
        fields[copy.STATS_VOICE_PEAKS] = "\n".join(
            f"{row.period_start:%Y-%m-%d} — **{row.count}**" for row in stats.top_voice_days[:5]
        )
    if stats.peak_hours and stats.peak_hours[0][1] > 0:
        fields[copy.STATS_HOURS] = " · ".join(f"{hour:02d}:00 **{count}**" for hour, count in stats.peak_hours[:6])
    fields[copy.STATS_AUTHORS] = copy.stats_authors(
        today=stats.unique_today, week=stats.unique_7d, zone=zone
    )
    if pairs:
        fields[copy.STATS_DENSE] = "\n".join(
            pair_line(guild, pair.left_id, pair.right_id, pair.strength, pair.events) for pair in pairs[:5]
        )
    return fields


def members_fields(guild: discord.Guild, stats: Any) -> dict[str, object]:
    fields: dict[str, object] = {}
    if stats.top_members:
        lines = [f"{user_name(guild, user_id)} — **{count}**" for user_id, count in stats.top_members]
        fields[copy.STATS_TOP_MESSAGES] = "\n".join(lines)
    else:
        fields[copy.STATS_TOP_MESSAGES] = copy.STATS_MEMBERS_EMPTY
    if stats.tenure:
        fields[copy.STATS_TENURE] = "\n".join(f"{label} — **{count}**" for label, count in stats.tenure)
    return fields


def channels_fields(guild: discord.Guild, ranks: Sequence[tuple[int, int, int]]) -> dict[str, object]:
    lines = [
        f"{channel_name(guild, channel_id)} — **{messages}** · авторов **{authors}**"
        for channel_id, messages, authors in ranks
    ]
    return {copy.STATS_BY_HISTORY: "\n".join(lines)}


def person_fields(guild: discord.Guild, dossier: Any) -> dict[str, object]:
    fields: dict[str, object] = {
        copy.STATS_SENT: (
            f"сообщения **{dossier.messages}**\n"
            f"файлы **{dossier.attachments}** · стикеры **{dossier.stickers}** · ссылки **{dossier.links}**"
        ),
        copy.STATS_LINKS: (
            f"ответы: дал {dossier.replies_given} / получил {dossier.replies_received}\n"
            f"упоминания: дал {dossier.mentions_given} / получил {dossier.mentions_received}\n"
            f"реакции: дал {dossier.reactions_given} / получил {dossier.reactions_received}"
        ),
    }
    if dossier.first_message_at:
        fields[copy.STATS_FIRST] = stamp(dossier.first_message_at)
    if dossier.last_message_at:
        fields[copy.STATS_LAST] = stamp(dossier.last_message_at)
    if dossier.channels:
        fields[copy.STATS_WHERE] = "\n".join(
            f"{channel_name(guild, channel_id)} — **{count}**" for channel_id, count in dossier.channels
        )
    if dossier.partners:
        fields[copy.STATS_WITH] = "\n".join(partner_line(guild, other_id, pair) for other_id, pair in dossier.partners)
    elif dossier.messages == 0:
        fields[copy.STATS_NO_TRACE_FIELD] = copy.STATS_NO_TRACE
    return fields


def guild_ties_fields(guild: discord.Guild, pairs: Sequence[Any]) -> tuple[str, dict[str, object] | None]:
    if not pairs:
        return copy.STATS_TIES_EMPTY, None
    fields = {
        copy.STATS_TOP_PAIRS: "\n".join(
            pair_line(guild, pair.left_id, pair.right_id, pair.strength, pair.events) for pair in pairs[:12]
        )
    }
    return copy.STATS_TIES_WEIGHT, fields


def person_ties_fields(
    guild: discord.Guild,
    pairs: Sequence[Any],
    user_id: int,
) -> tuple[str, dict[str, object] | None]:
    partners = partners_for(list(pairs), user_id)[:12]
    if not partners:
        return copy.STATS_TIES_NONE, None
    fields = {copy.STATS_PARTNERS: "\n".join(partner_line(guild, other_id, pair) for other_id, pair in partners)}
    return "", fields


def _tab_row(tab: str, on_tab: TabSwitch) -> ui.ActionRow:
    select = ui.Select(
        placeholder=copy.STATS_TABS,
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=label, value=value, default=value == tab) for value, label in TABS],
    )

    async def picked(interaction: discord.Interaction) -> None:
        await on_tab(interaction, select.values[0] if select.values else tab)

    select.callback = instant(picked)
    return ui.ActionRow(select)
