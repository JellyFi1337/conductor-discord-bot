"""Карточка /profile: шапка, репутация, активность. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import discord
from discord import ui

from conductor.domain.achievements import AchievementCard
from conductor.domain.ladder import LadderCard
from conductor.domain.reputation import RepDossier
from conductor.presentation import copy
from conductor.presentation.achievements import profile_lines
from conductor.presentation.interact import instant
from conductor.presentation.ladder import me_lines
from conductor.presentation.layout import panel
from conductor.presentation.reputation import dossier_lines
from conductor.presentation.tokens import Icon, Tone

TAB_REP = "rep"
TAB_ACTIVITY = "activity"
TAB_ACHIEVEMENTS = "achievements"

TabSwitch = Callable[[discord.Interaction, str], Awaitable[None]]


def rep_level(dossier: RepDossier) -> str:
    name = dossier.tier_name
    if name:
        return copy.PROFILE_REP_TIER.format(name=name, score=dossier.score)
    if dossier.sinner:
        return copy.PROFILE_REP_TIER.format(name=copy.REP_TIER_DEFAULTS[0], score=dossier.score)
    return str(dossier.score)


def profile_card(
    *,
    name: str,
    tenure_days: int,
    card: LadderCard,
    dossier: RepDossier,
    tab: str,
    on_tab: TabSwitch | None = None,
    staff_quota: str | None = None,
    on_dossier: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    role_lines: Sequence[str] = (),
    lfg_posts: int = 0,
    achievements: AchievementCard | None = None,
) -> ui.LayoutView:
    header = (
        f"{copy.PROFILE_TENURE} **{tenure_days}** {copy.LADDER_DAYS} · "
        f"{copy.ladder_step(card.held)} · {copy.PROFILE_REP} **{rep_level(dossier)}**"
    )
    body = [header]
    if staff_quota:
        body.append(f"{copy.PROFILE_STAFF} · {staff_quota}")
    body.extend(role_lines)
    if lfg_posts:
        body.append(copy.lfg_author_line(lfg_posts))
    body.append("")
    if tab == TAB_ACTIVITY:
        body.extend(me_lines(card))
        footnote = copy.ladder_footnote(card)
    elif tab == TAB_ACHIEVEMENTS:
        shown = achievements or AchievementCard(user_id=0, lines=())
        body.extend(profile_lines(shown))
        footnote = copy.ACHIEVEMENT_FOOTNOTE
    else:
        body.extend(dossier_lines(dossier))
        footnote = copy.rep_footnote(dossier.tier, dry_run=dossier.dry_run)
    rows: tuple[ui.ActionRow, ...] = (_tab_row(tab, on_tab),) if on_tab is not None else ()
    if on_dossier is not None:
        button = ui.Button(label=copy.DOSSIER_OPEN, style=discord.ButtonStyle.secondary)

        async def pressed(interaction: discord.Interaction) -> None:
            await on_dossier(interaction)

        button.callback = instant(pressed)
        rows = (*rows, ui.ActionRow(button))
    return panel(
        name,
        body,
        tone=Tone.SPECIAL,
        footnote=footnote,
        rows=rows,
        icon=Icon.STAR,
    )


def _tab_row(tab: str, on_tab: TabSwitch) -> ui.ActionRow:
    options = (
        (TAB_REP, copy.PROFILE_TAB_REP),
        (TAB_ACTIVITY, copy.PROFILE_TAB_ACTIVITY),
        (TAB_ACHIEVEMENTS, copy.PROFILE_TAB_ACHIEVEMENTS),
    )
    select = ui.Select(
        placeholder=copy.PROFILE_TABS,
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=label, value=value, default=value == tab) for value, label in options],
    )

    async def picked(interaction: discord.Interaction) -> None:
        await on_tab(interaction, select.values[0] if select.values else tab)

    select.callback = instant(picked)
    return ui.ActionRow(select)
