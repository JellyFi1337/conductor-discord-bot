"""Карточки /data export и /data forget. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import ui

from conductor.presentation import copy
from conductor.presentation.interact import acked
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone

ForgetClick = Callable[[discord.Interaction], Awaitable[None]]


def forget_done(*, rows: int, cache: int) -> ui.LayoutView:
    tone = Tone.MUTED if rows <= 0 and cache <= 0 else Tone.OK
    return panel(copy.DATA_FORGET_TITLE, copy.data_forget_body(rows=rows, cache=cache), tone=tone)


def forget_cancelled() -> ui.LayoutView:
    return panel(copy.DATA_FORGET_TITLE, copy.DATA_FORGET_CANCELLED, tone=Tone.MUTED)


def forget_confirm(*, on_yes: ForgetClick, on_no: ForgetClick) -> ui.LayoutView:
    yes = ui.Button(label=copy.DATA_FORGET_VERB, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.DATA_FORGET_CONFIRM,
        copy.DATA_FORGET_HINT,
        tone=Tone.DANGER,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )
