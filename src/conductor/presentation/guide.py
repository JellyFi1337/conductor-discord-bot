"""Карточка /help: вкладки, страницы, пометка команд и кнопок."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import discord
from discord import ui

from conductor.domain.access import ACTION_DEFAULTS, STAFF_RANKS, AccessSnapshot, Rank, can
from conductor.domain.guide import (
    KIND_BUTTON,
    TAB_MEMO,
    TAB_MINE,
    TAB_START,
    GuideCommand,
)
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Tone

__all__ = ["help_card", "help_who", "memo_body", "mine_body"]

TabSwitch = Callable[[discord.Interaction, str], Awaitable[None]]
PageSwitch = Callable[[discord.Interaction, int], Awaitable[None]]


def help_who(entry: GuideCommand, snapshot: AccessSnapshot | None = None) -> str:
    if entry.who is not None:
        return copy.HELP_WHO.get(entry.who, copy.HELP_WHO_ALL)
    if entry.key is None:
        return copy.HELP_WHO_ALL
    allowed = snapshot.rule(entry.key).ranks if snapshot is not None else ACTION_DEFAULTS[entry.key]
    if can(Rank.NONE, allowed=allowed):
        return copy.HELP_WHO_ALL
    for rank in STAFF_RANKS:
        if can(rank, allowed=allowed):
            if rank is Rank.SPECIAL:
                return copy.RANK_SPECIAL
            return copy.help_who_from(rank.key)
    return copy.RANK_SPECIAL


def help_mark(entry: GuideCommand) -> str:
    if entry.kind == KIND_BUTTON:
        return f"{copy.HELP_MARK_BUTTON} {entry.path}"
    return entry.path


def help_entry_line(entry: GuideCommand) -> str:
    return f"**{help_mark(entry)}** — {entry.summary}"


def mine_body(entries: Sequence[GuideCommand]) -> str:
    lines: list[str] = []
    last = ""
    for item in entries:
        if item.section != last:
            label = copy.HELP_SECTION_LABELS.get(item.section, item.section)
            lines.append(f"**{label}**")
            last = item.section
        lines.append(help_entry_line(item))
    return "\n".join(lines) if lines else copy.HELP_EMPTY


def memo_body(entries: Sequence[GuideCommand]) -> str:
    blocks: list[str] = []
    for item in entries:
        part = [help_entry_line(item)]
        part.extend(f"{name} · {desc}" for name, desc in item.params)
        part.extend(item.details)
        blocks.append("\n".join(part))
    return "\n\n".join(blocks) if blocks else copy.HELP_EMPTY


def help_heading(*, tab: str, rank: Rank, named_rank: bool) -> str:
    if tab == TAB_MEMO:
        return copy.help_title(copy.HELP_TAB_MEMO)
    if tab == TAB_START:
        return copy.help_title(copy.HELP_TAB_START)
    if named_rank and rank is not Rank.NONE:
        return copy.help_title(copy.rank_label(rank.key))
    return copy.help_title(copy.HELP_TAB_MINE)


def help_card(
    *,
    tab: str,
    rank: Rank,
    body: str,
    page: int = 1,
    pages: int = 1,
    named_rank: bool = False,
    show_start: bool = False,
    on_tab: TabSwitch | None = None,
    on_page: PageSwitch | None = None,
) -> ui.LayoutView:
    rows: list[ui.ActionRow] = []
    if on_tab is not None:
        rows.append(_tab_row(tab, rank, show_start=show_start, on_tab=on_tab))
    if on_page is not None and pages > 1:
        rows.append(_page_row(rank, page, pages, on_page))
    footnote = copy.HELP_FOOTNOTE
    if pages > 1:
        footnote = f"{copy.HELP_FOOTNOTE} · {copy.help_page_label(page, pages)}"
    return panel(
        help_heading(tab=tab, rank=rank, named_rank=named_rank),
        body,
        tone=Tone.INFO,
        footnote=footnote,
        rows=tuple(rows),
    )


def _tab_row(tab: str, rank: Rank, *, show_start: bool, on_tab: TabSwitch) -> ui.ActionRow:
    buttons = [
        _tab_button(TAB_MINE, copy.HELP_TAB_MINE, tab, rank, on_tab),
        _tab_button(TAB_MEMO, copy.HELP_TAB_MEMO, tab, rank, on_tab),
    ]
    if show_start:
        buttons.append(_tab_button(TAB_START, copy.HELP_TAB_START, tab, rank, on_tab))
    return ui.ActionRow(*buttons)


def _tab_button(
    value: str,
    label: str,
    current: str,
    rank: Rank,
    on_tab: TabSwitch,
) -> ui.Button:
    button = ui.Button(
        label=label,
        style=discord.ButtonStyle.primary if value == current else discord.ButtonStyle.secondary,
        disabled=value == current,
        custom_id=f"c:hp:{value}:{int(rank)}",
    )

    async def pressed(interaction: discord.Interaction) -> None:
        await on_tab(interaction, value)

    button.callback = instant(pressed)
    return button


def _page_row(rank: Rank, page: int, pages: int, on_page: PageSwitch) -> ui.ActionRow:
    prev_btn = ui.Button(
        label=copy.HELP_PAGE_PREV,
        style=discord.ButtonStyle.secondary,
        disabled=page <= 1,
        custom_id=f"c:hp:pg:{int(rank)}:{page - 1}",
    )
    next_btn = ui.Button(
        label=copy.HELP_PAGE_NEXT,
        style=discord.ButtonStyle.secondary,
        disabled=page >= pages,
        custom_id=f"c:hp:pg:{int(rank)}:{page + 1}",
    )

    async def go_prev(interaction: discord.Interaction) -> None:
        await on_page(interaction, page - 1)

    async def go_next(interaction: discord.Interaction) -> None:
        await on_page(interaction, page + 1)

    prev_btn.callback = instant(go_prev)
    next_btn.callback = instant(go_next)
    return ui.ActionRow(prev_btn, next_btn)
