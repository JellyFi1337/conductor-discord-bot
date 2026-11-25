"""Карточка мастера сопоставления существующих каналов и ролей."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import discord
from discord import ui

from conductor.domain.mapping import MappingDecision, MappingRow, mapping_row_order
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone

__all__ = [
    "ROWS_PER_PAGE",
    "mapping_card",
    "mapping_pages",
    "mapping_stale_card",
]

ROWS_PER_PAGE = 4
_VALUE_CREATE = "create"
_VALUE_SKIP = "skip"
_VALUE_KEEP = "keep"

MappingChoose = Callable[[discord.Interaction, str, MappingDecision, int | None], Awaitable[None]]
MappingPage = Callable[[discord.Interaction, int], Awaitable[None]]
MappingAction = Callable[[discord.Interaction], Awaitable[None]]


def mapping_pages(rows: Sequence[MappingRow], *, with_old: bool = False) -> tuple[tuple[MappingRow, ...], ...]:
    del with_old
    by_key = {row.key: row for row in rows}
    ordered = [by_key[key] for key in _row_keys(rows) if key in by_key]
    pages = [tuple(ordered[index : index + ROWS_PER_PAGE]) for index in range(0, len(ordered), ROWS_PER_PAGE)]
    return tuple(pages) if pages else ((),)


def mapping_stale_card(*, on_rebuild: MappingAction) -> ui.LayoutView:
    rebuild = ui.Button(label=copy.SETUP_MAP_STALE_VERB, style=discord.ButtonStyle.primary)
    rebuild.callback = instant(on_rebuild)
    return panel(
        copy.SETUP_MAP_TITLE,
        copy.SETUP_MAP_STALE,
        tone=Tone.WARN,
        icon=Icon.WARN,
        rows=(ui.ActionRow(rebuild),),
        timeout=300.0,
    )


def mapping_card(
    rows: Sequence[MappingRow],
    *,
    page: int = 1,
    with_old: bool = False,
    on_choose: MappingChoose,
    on_page: MappingPage,
    on_apply: MappingAction,
    on_reset: MappingAction,
    on_back: MappingAction,
) -> ui.LayoutView:
    pages = mapping_pages(rows, with_old=with_old)
    total = max(1, len(pages))
    current = max(1, min(int(page), total))
    chunk = pages[current - 1] if pages and pages[0] else ()
    lines = [_row_line(row) for row in chunk] or [copy.SETUP_MAP_STALE]
    body = "\n".join((*lines, "", copy.setup_map_page(current, total), copy.SETUP_MAP_HINT))
    rows_ui: list[ui.ActionRow] = [_select_row(row, on_choose=on_choose) for row in chunk]
    nav: list[ui.Button] = []
    if total > 1:
        prev = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=current <= 1)
        nxt = ui.Button(label="→", style=discord.ButtonStyle.secondary, disabled=current >= total)

        async def go_prev(click: discord.Interaction) -> None:
            await on_page(click, current - 1)

        async def go_next(click: discord.Interaction) -> None:
            await on_page(click, current + 1)

        prev.callback = instant(go_prev)
        nxt.callback = instant(go_next)
        nav.extend((prev, nxt))
    apply_btn = ui.Button(label=copy.SETUP_MAP_APPLY, style=discord.ButtonStyle.success)
    reset_btn = ui.Button(label=copy.SETUP_MAP_RESET, style=discord.ButtonStyle.secondary)
    back_btn = ui.Button(label=copy.SETUP_MAP_BACK, style=discord.ButtonStyle.secondary)
    apply_btn.callback = instant(on_apply)
    reset_btn.callback = instant(on_reset)
    back_btn.callback = instant(on_back)
    nav.extend((apply_btn, reset_btn, back_btn))
    rows_ui.append(ui.ActionRow(*nav[:5]))
    return panel(
        copy.SETUP_MAP_TITLE,
        body,
        tone=Tone.INFO,
        icon=Icon.PANEL,
        rows=tuple(rows_ui),
        timeout=900.0,
    )


def _row_keys(rows: Sequence[MappingRow]) -> tuple[str, ...]:
    wanted = {row.key for row in rows}
    return tuple(key for key in mapping_row_order(with_old=True) if key in wanted)


def _row_line(row: MappingRow) -> str:
    if row.decision is MappingDecision.BIND:
        chosen = next((item.name for item in row.candidates if item.object_id == row.chosen_id), "")
        auto = f"{Icon.DONE} {row.label} · {copy.SETUP_MAP_AUTO}"
        return f"{auto}\n-# {chosen}" if chosen else auto
    if row.decision is MappingDecision.KEEP:
        return f"{Icon.DONE} {row.label} · {copy.SETUP_MAP_KEEP}"
    if row.decision is MappingDecision.SKIP:
        return f"{row.label} · {copy.SETUP_MAP_SKIP}"
    return f"{row.label} · {copy.SETUP_MAP_CREATE}"


def _select_row(row: MappingRow, *, on_choose: MappingChoose) -> ui.ActionRow:
    options = [
        discord.SelectOption(
            label=copy.SETUP_MAP_CREATE[:100],
            value=_VALUE_CREATE,
            default=row.decision is MappingDecision.CREATE,
        )
    ]
    if row.bound_id is not None:
        options.append(
            discord.SelectOption(
                label=copy.SETUP_MAP_KEEP[:100],
                value=_VALUE_KEEP,
                default=row.decision is MappingDecision.KEEP,
            )
        )
    for item in row.candidates[:5]:
        options.append(
            discord.SelectOption(
                label=item.name[:100],
                value=str(item.object_id),
                description=copy.setup_map_score(item.reason, item.score)[:100],
                default=row.decision is MappingDecision.BIND and row.chosen_id == item.object_id,
            )
        )
    options.append(
        discord.SelectOption(
            label=copy.SETUP_MAP_SKIP[:100],
            value=_VALUE_SKIP,
            default=row.decision is MappingDecision.SKIP,
        )
    )
    placeholder = row.label[:150]
    select = ui.Select(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def picked(click: discord.Interaction) -> None:
        value = select.values[0] if select.values else _VALUE_CREATE
        decision, object_id = _parse_choice(value, row)
        await on_choose(click, row.key, decision, object_id)

    select.callback = instant(picked)
    return ui.ActionRow(select)


def _parse_choice(value: str, row: MappingRow) -> tuple[MappingDecision, int | None]:
    if value == _VALUE_CREATE:
        return MappingDecision.CREATE, None
    if value == _VALUE_SKIP:
        return MappingDecision.SKIP, None
    if value == _VALUE_KEEP:
        return MappingDecision.KEEP, row.bound_id
    try:
        return MappingDecision.BIND, int(value)
    except ValueError:
        return MappingDecision.CREATE, None
