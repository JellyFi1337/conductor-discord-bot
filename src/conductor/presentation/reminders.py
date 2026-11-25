"""Карточки напоминания. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import discord
from discord import ui

from conductor.domain.reminders import ReminderCard, excerpt
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import empty, listing, panel
from conductor.presentation.mentions import going_mentions
from conductor.presentation.motion import Countdown
from conductor.presentation.tokens import Icon, Tone

__all__ = ["cancelled_view", "created_view", "due_mentions", "due_view", "list_view"]

CancelCallback = Callable[[discord.Interaction, int], Awaitable[None]]


def due_mentions(user_id: int) -> discord.AllowedMentions:
    return going_mentions((int(user_id),))


def created_view(card: ReminderCard) -> ui.LayoutView:
    when = Countdown().render(card.due_at)
    return panel(
        copy.REMIND_TITLE,
        copy.REMIND_SAVED,
        fields={
            copy.REMIND_NUMBER: f"#{card.reminder_id}",
            copy.REMIND_WHEN: when,
            copy.REMIND_TEXT: card.text,
        },
        footnote=copy.REMIND_FOOTNOTE,
        tone=Tone.OK,
        icon=Icon.WAIT,
    )


def list_view(
    cards: Sequence[ReminderCard],
    *,
    on_cancel: CancelCallback | None = None,
) -> ui.LayoutView:
    if not cards:
        return empty(copy.REMIND_EMPTY, hint=copy.REMIND_EMPTY_HINT)
    rows = [
        copy.remind_list_line(card.reminder_id, excerpt(card.text), Countdown().render(card.due_at)) for card in cards
    ]
    buttons: list[ui.ActionRow] = []
    if on_cancel is not None:
        chunk: list[ui.Button] = []
        for card in cards:
            button = ui.Button(
                label=copy.remind_cancel_label(card.reminder_id),
                style=discord.ButtonStyle.danger,
            )
            reminder_id = card.reminder_id

            async def callback(
                interaction: discord.Interaction,
                reminder_id: int = reminder_id,
            ) -> None:
                await on_cancel(interaction, reminder_id)

            button.callback = instant(callback)
            chunk.append(button)
            if len(chunk) == 5:
                buttons.append(ui.ActionRow(*chunk))
                chunk = []
        if chunk:
            buttons.append(ui.ActionRow(*chunk))
    return panel(
        copy.REMIND_LIST_TITLE,
        listing(rows, 1, 1),
        footnote=copy.REMIND_FOOTNOTE,
        tone=Tone.INFO,
        icon=Icon.WAIT,
        rows=tuple(buttons),
    )


def cancelled_view(card: ReminderCard) -> ui.LayoutView:
    return panel(
        copy.REMIND_TITLE,
        copy.REMIND_CANCELLED,
        fields={
            copy.REMIND_NUMBER: f"#{card.reminder_id}",
            copy.REMIND_TEXT: card.text,
        },
        tone=Tone.OK,
    )


def due_view(card: ReminderCard) -> ui.LayoutView:
    quote = "\n".join(f"> {line}" if line else ">" for line in card.text.split("\n")[:8])
    return panel(
        copy.REMIND_DUE,
        (f"<@{card.user_id}>", quote),
        fields={copy.REMIND_WHEN: Countdown().render(card.due_at)},
        tone=Tone.WARN,
        icon=Icon.WAIT,
        mentions=due_mentions(card.user_id),
        timeout=None,
    )
